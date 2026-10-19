"""``jobhunt ingest``: validate AI results in work/results/ and commit the accepted ones."""

from __future__ import annotations

import json
from pathlib import Path

import click
from pydantic import BaseModel, Field

from jobhunt.cli.discovery import FIXTURES_HELP, _prepare, make_client
from jobhunt.cli.fit import _inputs, _lock
from jobhunt.cli.framework import Result, agent_command
from jobhunt.discovery.text import utc_now
from jobhunt.envelope import ErrorInfo, paginate
from jobhunt.errors import ExitCode
from jobhunt.fit.engine import run_match
from jobhunt.fit.recall import make_embedder
from jobhunt.fit.tasks import TASK_KIND
from jobhunt.ingest import essays, judgment, outreach, research  # noqa: F401  (handlers)
from jobhunt.ingest.core import MAX_ATTEMPTS, Ctx, IngestReport, ingest
from jobhunt.store import open_store

REFIT_KINDS = {TASK_KIND}


class TaskRow(BaseModel):
    id: str
    kind: str
    job_id: int | None
    company: str | None
    title: str | None
    path: str | None
    result_path: str | None
    attempts: int
    last_errors: list[dict[str, str]] = Field(default_factory=list)


class TasksList(BaseModel):
    tasks: list[TaskRow]
    by_kind: dict[str, int]


def register(cli: click.Group) -> None:
    @agent_command(cli, "tasks", output=TasksList)
    @click.option("--kind", default=None, help="Only this task kind (e.g. requirement_evidence).")
    @click.option("--job", "job_id", type=int, default=None, help="Only this job's tasks.")
    @click.option("--limit", type=int, default=20, help="Tasks per page.")
    @click.option("--cursor", default=None, help="Page cursor from a previous call.")
    def tasks_cmd(kind: str | None, job_id: int | None, limit: int, cursor: str | None) -> Result:
        """List pending judgment tasks: file to read, where to write the result, past errors."""
        paths, _ = _prepare(True)
        where, params = ["t.status='pending'"], []
        if kind:
            where.append("t.kind=?")
            params.append(kind)
        if job_id is not None:
            where.append("t.job_id=?")
            params.append(job_id)
        with open_store(paths.db, dry_run=True) as conn:
            rows = conn.execute(
                "SELECT t.*, j.company, j.title FROM tasks t LEFT JOIN jobs j ON j.id=t.job_id "
                "LEFT JOIN fits f ON f.job_id=t.job_id "
                f"WHERE {' AND '.join(where)} ORDER BY coalesce(f.priority, 0) DESC, t.created_at",
                params,
            ).fetchall()
        chunk, page = paginate(rows, cursor, limit)
        by_kind: dict[str, int] = {}
        for r in rows:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        out = [
            TaskRow(
                id=r["id"],
                kind=r["kind"],
                job_id=r["job_id"],
                company=r["company"],
                title=r["title"],
                path=r["path"],
                result_path=str(paths.results / f"{r['id']}.json"),
                attempts=r["attempts"],
                last_errors=json.loads(r["last_error"]) if r["last_error"] else [],
            )
            for r in chunk
            if not r["path"] or Path(r["path"]).exists()
        ]
        nxt = ["Do each task, write its result to result_path, then run `jobhunt ingest`."]
        return Result(
            data=TasksList(tasks=out, by_kind=by_kind), page=page, next_actions=nxt if out else []
        )

    @agent_command(cli, "ingest", output=IngestReport, writes=True)
    @click.option(
        "--task", "tasks", multiple=True, help="Only this task id (repeatable). Default: all."
    )
    @click.option(
        "--fixtures",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help="Fetch cited pages from this fixture directory. " + FIXTURES_HELP,
    )
    def ingest_cmd(tasks: tuple[str, ...], fixtures: Path | None, dry_run: bool) -> Result:
        """Validate AI results in ~/.jobhunt/work/results/ and commit the ones that pass.

        Every posting quote is checked against the posting and every claim about you against a
        profile line; research quotes are re-checked on the pages they cite. A rejected result
        gets <task_id>.errors.json next to it, with a JSON path and a fix for each problem;
        correct the file and run ingest again. Accepted judgment results recompute the job's
        band, which stops being provisional once nothing is unjudged.
        """
        paths, cfg = _prepare(dry_run)
        now = utc_now()
        ctx = Ctx(
            paths=paths,
            now=now,
            load_inputs=lambda: _inputs(paths, cfg, now),
            make_client=lambda: make_client(cfg, fixtures),
            dry_run=dry_run,
        )
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            rep = ingest(conn, ctx, only=set(tasks) or None, dry_run=dry_run)
            refit_ids = {a.job_id for a in rep.accepted if a.kind in REFIT_KINDS and a.job_id}
            if refit_ids:
                embedder, note = make_embedder(cfg.fit)
                run_match(
                    conn,
                    ctx.inputs,
                    tasks_dir=paths.tasks,
                    results_dir=paths.results,
                    embedder=embedder,
                    embed_note=note,
                    dry_run=dry_run,
                )
                rows = conn.execute(
                    f"SELECT job_id, band, provisional, priority FROM fits WHERE job_id IN "
                    f"({','.join('?' * len(refit_ids))}) ORDER BY job_id",
                    sorted(refit_ids),
                ).fetchall()
                rep.refit = {
                    str(r["job_id"]): {
                        "band": r["band"],
                        "provisional": bool(r["provisional"]),
                        "priority": r["priority"],
                    }
                    for r in rows
                }
        nxt: list[str] = []
        user: list[str] = []
        if rep.rejected:
            nxt.append(
                "Fix each rejected result using its errors file, then run `jobhunt ingest` again."
            )
            stuck = [r.task_id for r in rep.rejected if r.attempts >= MAX_ATTEMPTS]
            if stuck:
                user.append(
                    f"{len(stuck)} task(s) were rejected {MAX_ATTEMPTS}+ times "
                    f"({', '.join(stuck)}); look at them with `jobhunt show <job>` yourself."
                )
        if rep.accepted:
            nxt.append("Run `jobhunt sync` to update the tracker.")
        if rep.pending_tasks:
            nxt.append(f"{rep.pending_tasks} task(s) still pending in {paths.tasks}.")
        if rep.rejected:
            return Result(
                data=rep,
                ok=False,
                error=ErrorInfo(
                    code="results_rejected",
                    message=f"{len(rep.rejected)} result(s) failed validation; "
                    f"{len(rep.accepted)} accepted.",
                    details={},
                ),
                exit_code=ExitCode.VALIDATION,
                next_actions=nxt,
                user_actions=user,
            )
        return Result(data=rep, next_actions=nxt, user_actions=user)
