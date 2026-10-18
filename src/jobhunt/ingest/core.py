"""Scan ``work/results/``, validate each result against its task, commit the accepted ones.

A result is ``<task_id>.json``. Accepted results move to ``results/ingested/``. A rejected one stays
where it is, its task stays pending (attempts + 1), and ``<task_id>.errors.json`` lists every
problem with a JSON path so the AI can fix the file and run ``jobhunt ingest`` again.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from jobhunt.fit.engine import Inputs
from jobhunt.ingest.verify import Problem
from jobhunt.paths import Paths, secure_file

ERRORS_SUFFIX = ".errors.json"
MAX_ATTEMPTS = 3


@dataclass
class Ctx:
    """What handlers need. ``inputs`` is loaded on first use (profile, preferences, answers)."""

    paths: Paths
    now: str
    load_inputs: Callable[[], Inputs]
    _inputs: Inputs | None = field(default=None, repr=False)

    @property
    def inputs(self) -> Inputs:
        if self._inputs is None:
            self._inputs = self.load_inputs()
        return self._inputs


class Handler(Protocol):
    kind: str

    def check(
        self, conn: sqlite3.Connection, raw: dict[str, Any], task: sqlite3.Row, ctx: Ctx
    ) -> tuple[Any, list[Problem]]: ...

    def apply(
        self, conn: sqlite3.Connection, parsed: Any, task: sqlite3.Row, ctx: Ctx
    ) -> dict[str, Any]: ...


HANDLERS: dict[str, Handler] = {}


def register(handler: Handler) -> Handler:
    HANDLERS[handler.kind] = handler
    return handler


def schema_problems(err: ValidationError) -> list[Problem]:
    out = []
    for e in err.errors():
        path = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in e["loc"])
        out.append(Problem(path, e["msg"]))
    return out


class Accepted(BaseModel):
    task_id: str
    kind: str
    job_id: int | None
    summary: dict[str, Any] = Field(default_factory=dict)


class Rejected(BaseModel):
    task_id: str
    file: str
    attempts: int
    errors: list[dict[str, str]]
    errors_file: str | None = None


class Skipped(BaseModel):
    file: str
    reason: str


class IngestReport(BaseModel):
    dry_run: bool
    accepted: list[Accepted] = Field(default_factory=list)
    rejected: list[Rejected] = Field(default_factory=list)
    skipped: list[Skipped] = Field(default_factory=list)
    pending_tasks: int = 0
    refit: dict[str, Any] | None = Field(
        None, description="Bands after recomputing the jobs whose judgments were accepted."
    )


def result_files(results_dir: Path, only: set[str] | None = None) -> list[Path]:
    if not results_dir.is_dir():
        return []
    files = [
        p
        for p in sorted(results_dir.glob("*.json"))
        if not p.name.endswith(ERRORS_SUFFIX) and not p.name.endswith(".tmp")
    ]
    return [p for p in files if only is None or p.stem in only]


def _reject(
    conn: sqlite3.Connection,
    report: IngestReport,
    f: Path,
    task_id: str,
    problems: list[Problem],
    *,
    dry_run: bool,
) -> None:
    attempts = 0
    row = conn.execute("SELECT attempts FROM tasks WHERE id=?", (task_id,)).fetchone()
    errors = [p.as_dict() for p in problems]
    if row is not None:
        attempts = row["attempts"] + 1
        conn.execute(
            "UPDATE tasks SET attempts=?, last_error=? WHERE id=?",
            (attempts, json.dumps(errors), task_id),
        )
    err_file = f.with_name(f.stem + ERRORS_SUFFIX)
    if not dry_run:
        err_file.write_text(
            json.dumps({"task_id": task_id, "errors": errors}, indent=2), encoding="utf-8"
        )
        secure_file(err_file)
    report.rejected.append(
        Rejected(
            task_id=task_id,
            file=str(f),
            attempts=attempts,
            errors=errors,
            errors_file=None if dry_run else str(err_file),
        )
    )


def _archive(f: Path, task: sqlite3.Row, dry_run: bool) -> None:
    """Move the result and its task file into ``ingested/`` so only open work stays in view."""
    if dry_run:
        return
    for src in (f, Path(task["path"]) if task["path"] else None):
        if src is not None and src.exists():
            done = src.parent / "ingested"
            done.mkdir(exist_ok=True)
            src.replace(done / src.name)
    f.with_name(f.stem + ERRORS_SUFFIX).unlink(missing_ok=True)


def ingest(
    conn: sqlite3.Connection, ctx: Ctx, *, only: set[str] | None, dry_run: bool
) -> IngestReport:
    report = IngestReport(dry_run=dry_run)
    for f in result_files(ctx.paths.results, only):
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            _reject(
                conn, report, f, f.stem, [Problem("$", f"not valid JSON: {e}")], dry_run=dry_run
            )
            continue
        if not isinstance(raw, dict):
            _reject(
                conn, report, f, f.stem, [Problem("$", "must be a JSON object")], dry_run=dry_run
            )
            continue
        task_id = str(raw.get("task_id") or "")
        if task_id != f.stem:
            _reject(
                conn,
                report,
                f,
                f.stem,
                [Problem("$.task_id", f"must equal the file name's task id {f.stem!r}")],
                dry_run=dry_run,
            )
            continue
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            report.skipped.append(Skipped(file=str(f), reason=f"no task {task_id!r}"))
            continue
        if task["status"] == "ingested":
            _archive(f, task, dry_run)
            report.skipped.append(Skipped(file=str(f), reason="already ingested"))
            continue
        if task["status"] == "expired":
            newer = conn.execute(
                "SELECT id FROM tasks WHERE job_id IS ? AND kind=? AND status='pending'",
                (task["job_id"], task["kind"]),
            ).fetchone()
            hint = f"; do task {newer['id']} instead" if newer else ""
            report.skipped.append(
                Skipped(file=str(f), reason=f"task expired: its inputs changed{hint}")
            )
            continue
        handler = HANDLERS.get(task["kind"])
        if handler is None:
            report.skipped.append(Skipped(file=str(f), reason=f"no ingest for {task['kind']!r}"))
            continue
        if raw.get("job_id", task["job_id"]) != task["job_id"]:
            _reject(
                conn,
                report,
                f,
                task_id,
                [Problem("$.job_id", f"must be {task['job_id']} (the task's job)")],
                dry_run=dry_run,
            )
            continue
        parsed, problems = handler.check(conn, raw, task, ctx)
        if problems:
            _reject(conn, report, f, task_id, problems, dry_run=dry_run)
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            summary = handler.apply(conn, parsed, task, ctx)
            conn.execute(
                "UPDATE tasks SET status='ingested', ingested_at=?, last_error=NULL WHERE id=?",
                (ctx.now, task_id),
            )
            conn.execute(
                "INSERT INTO events (job_id, kind, at, details_json) VALUES (?, 'note', ?, ?)",
                (
                    task["job_id"],
                    ctx.now,
                    json.dumps({"type": "ingested", "task_id": task_id, "kind": task["kind"]}),
                ),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        _archive(f, task, dry_run)
        report.accepted.append(
            Accepted(task_id=task_id, kind=task["kind"], job_id=task["job_id"], summary=summary)
        )
    report.pending_tasks = conn.execute(
        "SELECT count(*) FROM tasks WHERE status='pending'"
    ).fetchone()[0]
    return report
