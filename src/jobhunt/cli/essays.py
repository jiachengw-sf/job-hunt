"""``jobhunt essays``: list prompts, write draft and review tasks, show drafts, approve yours."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
from pydantic import BaseModel, Field

from jobhunt.cli.discovery import _prepare
from jobhunt.cli.fit import _inputs, _lock
from jobhunt.cli.framework import Result, agent_command
from jobhunt.discovery.text import utc_now
from jobhunt.drafts.essays import (
    REVIEW_KIND,
    draft_file,
    essay_prompts,
    read_user_text,
    write_draft_task,
    write_review_task,
)
from jobhunt.drafts.grounding import edit_ratio, job_sources
from jobhunt.envelope import paginate
from jobhunt.errors import NotFound, ValidationFailed
from jobhunt.fit.jobview import JobView, load_jobs
from jobhunt.ingest.verify import clean_text
from jobhunt.store import open_store

DRAFT_LABEL = "draft — edit before use"


class EssayField(BaseModel):
    field_id: str
    prompt: str
    required: bool
    max_length: int | None
    status: str = Field(description="none | draft | reviewed | approved")
    chars: int | None = None
    file: str | None = None
    edit_ratio: float | None = None


class EssayJob(BaseModel):
    job_id: int
    company: str
    title: str
    deadline: str | None
    priority: float | None
    fields: list[EssayField]


class EssayList(BaseModel):
    jobs: list[EssayJob]


class TaskInfo(BaseModel):
    job_id: int
    status: str = Field(description="written | pending | reviewed | nothing_to_review")
    task_id: str | None = None
    task_path: str | None = None
    result_path: str | None = None


class DraftView(BaseModel):
    field_id: str
    prompt: str | None
    label: str = DRAFT_LABEL
    status: str
    text: str
    chars: int
    max_length: int | None
    file: str
    review_edits: list[dict[str, Any]] = Field(default_factory=list)
    edit_ratio: float | None = None


class DraftsShow(BaseModel):
    job_id: int
    drafts: list[DraftView]


class Approval(BaseModel):
    field_id: str
    chars: int
    edit_ratio: float
    unchanged: bool


class ApproveReport(BaseModel):
    job_id: int
    dry_run: bool
    approved: list[Approval]


def _job(conn: Any, job_id: int) -> JobView:
    jobs = load_jobs(conn, [job_id], live_only=False)
    if not jobs:
        raise NotFound(f"No job {job_id}.", next_actions=["Run `jobhunt essays list`."])
    return jobs[0]


def _fields(conn: Any, work: Path, job_id: int) -> list[EssayField]:
    drafts = {
        r["key"]: r
        for r in conn.execute("SELECT * FROM drafts WHERE job_id=? AND kind='essay'", (job_id,))
    }
    out = []
    for p in essay_prompts(conn, job_id):
        d = drafts.get(p.field_id)
        text = (d["approved_text"] or d["text"]) if d else None
        out.append(
            EssayField(
                field_id=p.field_id,
                prompt=p.label,
                required=p.required,
                max_length=p.max_length,
                status=d["status"] if d else "none",
                chars=len(text) if text else None,
                file=str(draft_file(work, job_id, p.field_id)) if d else None,
                edit_ratio=d["edit_ratio"] if d else None,
            )
        )
    return out


def register(cli: click.Group) -> None:
    @agent_command(cli, "essays list", output=EssayList)
    @click.option("--limit", type=int, default=10, help="Jobs per page.")
    @click.option("--cursor", default=None, help="Page cursor from a previous call.")
    def list_cmd(limit: int, cursor: str | None) -> Result:
        """Essay jobs by priority, with each prompt, its limit and its draft status."""
        paths, _ = _prepare(True)
        with open_store(paths.db, dry_run=True) as conn:
            rows = conn.execute(
                "SELECT j.id, j.company, j.title, j.deadline, f.priority FROM form_specs fs "
                "JOIN jobs j ON j.id=fs.job_id LEFT JOIN fits f ON f.job_id=j.id "
                "LEFT JOIN applications a ON a.job_id=j.id WHERE fs.class='essay' AND j.live=1 "
                "AND coalesce(a.status,'New') IN ('New','Prefilled') "
                "ORDER BY coalesce(f.priority,0) DESC, j.id"
            ).fetchall()
            chunk, page = paginate(rows, cursor, limit)
            jobs = [
                EssayJob(
                    job_id=r["id"],
                    company=r["company"],
                    title=r["title"],
                    deadline=r["deadline"],
                    priority=r["priority"],
                    fields=_fields(conn, paths.work, r["id"]),
                )
                for r in chunk
            ]
        nxt = (
            ["Draft one with `jobhunt essays draft <job>` (jobhunt-essays skill)."] if jobs else []
        )
        return Result(data=EssayList(jobs=jobs), page=page, next_actions=nxt)

    @agent_command(cli, "essays draft", output=TaskInfo, writes=True)
    @click.argument("job_id", type=int)
    def draft_cmd(job_id: int, dry_run: bool) -> Result:
        """Write an essay-draft task for a job: its prompts, limits, posting and citable lines.

        The AI drafts sentence by sentence, each citing a profile line, base answer, brief fact
        or posting quote; `jobhunt ingest` checks every citation and the character limits.
        """
        paths, cfg = _prepare(dry_run)
        now = utc_now()
        inp = _inputs(paths, cfg, now)
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            job = _job(conn, job_id)
            prompts = essay_prompts(conn, job_id)
            if not prompts:
                raise ValidationFailed(
                    f"Job {job_id} has no essay prompts on its form.",
                    next_actions=[f"Read its form first: `jobhunt inspect {job_id}`."],
                )
            sources = job_sources(conn, job.text, job.company, inp.profile, inp.answers)
            status, task_id, path = write_draft_task(
                conn, paths.tasks, paths.results, job, prompts, sources,
                style_skill=cfg.writing_style_skill, now=now, dry_run=dry_run,
            )  # fmt: skip
        result = paths.results / f"{task_id}.json"
        return Result(
            data=TaskInfo(job_id=job_id, status=status, task_id=task_id, task_path=str(path),
                          result_path=str(result)),
            next_actions=[f"Do the task in {path}, write {result}, then run `jobhunt ingest`."],
        )  # fmt: skip

    @agent_command(cli, "essays review", output=TaskInfo, writes=True)
    @click.argument("job_id", type=int)
    def review_cmd(job_id: int, dry_run: bool) -> Result:
        """Write the one review task for a job's drafts (do it in a fresh context).

        The reviewer returns exact {old, new, reason} edits that code applies. One round only.
        """
        paths, cfg = _prepare(dry_run)
        now = utc_now()
        inp = _inputs(paths, cfg, now)
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            job = _job(conn, job_id)
            drafts = conn.execute(
                "SELECT * FROM drafts WHERE job_id=? AND kind='essay' AND status='draft' "
                "ORDER BY key",
                (job_id,),
            ).fetchall()
            done = conn.execute(
                "SELECT count(*) FROM tasks WHERE job_id=? AND kind=? AND status='ingested'",
                (job_id, REVIEW_KIND),
            ).fetchone()[0]
            if not drafts:
                status = "reviewed" if done else "nothing_to_review"
                return Result(
                    data=TaskInfo(job_id=job_id, status=status),
                    next_actions=[f"Show the drafts: `jobhunt essays show {job_id}`."],
                )
            sources = job_sources(conn, job.text, job.company, inp.profile, inp.answers)
            status, task_id, path = write_review_task(
                conn, paths.tasks, paths.results, job, drafts, sources, now=now, dry_run=dry_run
            )
        result = paths.results / f"{task_id}.json"
        return Result(
            data=TaskInfo(job_id=job_id, status=status, task_id=task_id, task_path=str(path),
                          result_path=str(result)),
            next_actions=[
                f"In a fresh context, do the review task in {path}, write {result}, then run "
                "`jobhunt ingest`."
            ],
        )  # fmt: skip

    @agent_command(cli, "essays show", output=DraftsShow)
    @click.argument("job_id", type=int)
    def show_cmd(job_id: int) -> Result:
        """Show a job's essay drafts (marked "draft — edit before use") and where to edit them."""
        paths, _ = _prepare(True)
        with open_store(paths.db, dry_run=True) as conn:
            _job(conn, job_id)
            rows = conn.execute(
                "SELECT * FROM drafts WHERE job_id=? AND kind='essay' ORDER BY key", (job_id,)
            ).fetchall()
        import json

        out = [
            DraftView(
                field_id=r["key"],
                prompt=r["prompt"],
                label="approved by you" if r["status"] == "approved" else DRAFT_LABEL,
                status=r["status"],
                text=r["approved_text"] or r["text"],
                chars=len(r["approved_text"] or r["text"]),
                max_length=r["max_length"],
                file=str(draft_file(paths.work, job_id, r["key"])),
                review_edits=json.loads(r["review_json"] or "[]"),
                edit_ratio=r["edit_ratio"],
            )
            for r in rows
        ]
        user = []
        pending = [d for d in out if d.status != "approved"]
        if pending:
            user.append(
                f"Edit your draft(s) in {', '.join(d.file for d in pending)}, then run "
                f"`jobhunt essays approve {job_id}`. They're drafts: make them yours."
            )
        return Result(data=DraftsShow(job_id=job_id, drafts=out), user_actions=user)

    @agent_command(cli, "essays approve", output=ApproveReport, writes=True)
    @click.argument("job_id", type=int)
    @click.option("--field", "fields", multiple=True, help="Only this field id (repeatable).")
    @click.option(
        "--file",
        "file_",
        type=click.Path(dir_okay=False, exists=True, path_type=Path),
        default=None,
        help="Read your final text from this file (one --field only).",
    )
    def approve_cmd(
        job_id: int, fields: tuple[str, ...], file_: Path | None, dry_run: bool
    ) -> Result:
        """Approve your edited essay text so pre-fill can paste it; records how much you changed.

        Reads work/essays/<job>/<field>.txt (lines starting with # are ignored), strips hidden
        characters and checks the form's limit. Only you run this, after editing.
        """
        paths, cfg = _prepare(dry_run)
        now = utc_now()
        if file_ and len(fields) != 1:
            raise ValidationFailed("--file needs exactly one --field.")
        approved: list[Approval] = []
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            _job(conn, job_id)
            rows = conn.execute(
                "SELECT * FROM drafts WHERE job_id=? AND kind='essay' AND status IN "
                "('draft','reviewed','approved') ORDER BY key",
                (job_id,),
            ).fetchall()
            if fields:
                rows = [r for r in rows if r["key"] in fields]
                missing = set(fields) - {r["key"] for r in rows}
                if missing:
                    raise NotFound(f"No draft for {', '.join(sorted(missing))}.")
            if not rows:
                raise NotFound(
                    f"No essay drafts for job {job_id}.",
                    next_actions=[f"Draft them: `jobhunt essays draft {job_id}`."],
                )
            for r in rows:
                src = file_ or draft_file(paths.work, job_id, r["key"])
                if not src.exists():
                    raise NotFound(
                        f"{src} is missing.", next_actions=["Run `jobhunt essays show`."]
                    )
                text = clean_text(read_user_text(src)).strip()
                if not text:
                    raise ValidationFailed(f"{src} is empty.")
                if r["max_length"] and len(text) > r["max_length"]:
                    raise ValidationFailed(
                        f"{r['key']}: {len(text)} characters; the form allows {r['max_length']}.",
                        next_actions=[f"Shorten {src}, then approve again."],
                    )
                ratio = edit_ratio(r["text"], text)
                conn.execute(
                    "UPDATE drafts SET status='approved', approved_text=?, edit_ratio=?, "
                    "approved_at=?, updated_at=? WHERE id=?",
                    (text, ratio, now, now, r["id"]),
                )
                approved.append(
                    Approval(
                        field_id=r["key"],
                        chars=len(text),
                        edit_ratio=ratio,
                        unchanged=ratio < cfg.drafts.unchanged_edit_ratio,
                    )
                )
        user = []
        same = [a.field_id for a in approved if a.unchanged]
        if same:
            user.append(
                f"You approved {', '.join(same)} almost exactly as drafted. That's allowed, but "
                "answers in your own words usually land better; a few minutes of edits help."
            )
        return Result(
            data=ApproveReport(job_id=job_id, dry_run=dry_run, approved=approved),
            user_actions=user,
            next_actions=[f"Pre-fill can now paste these: `jobhunt prefill {job_id}`."],
        )
