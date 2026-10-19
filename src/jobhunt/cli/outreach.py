"""``jobhunt outreach``: what's due, draft tasks, show drafts, record what you sent.

jobhunt never sends anything. ``mark-sent`` only records that you sent a message yourself.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import click
from pydantic import BaseModel, Field

from jobhunt.cli.discovery import _prepare
from jobhunt.cli.essays import _job
from jobhunt.cli.fit import _inputs, _lock
from jobhunt.cli.framework import Result, agent_command
from jobhunt.discovery.text import utc_now
from jobhunt.drafts.grounding import job_sources
from jobhunt.drafts.outreach import (
    due_followups,
    followup_state,
    outreach_file,
    referral_candidates,
    write_outreach_task,
)
from jobhunt.errors import NotFound, ValidationFailed
from jobhunt.store import open_store

KINDS = ["referral", "coffee_chat", "followup"]


class DueReport(BaseModel):
    followups: list[dict[str, Any]]
    referrals: list[dict[str, Any]] = Field(
        description="Top-priority jobs with no referral contact: ask the user if they know anyone."
    )


class OutreachTask(BaseModel):
    job_id: int
    kind: str
    status: str
    task_id: str
    task_path: str
    result_path: str


class OutreachDraft(BaseModel):
    kind: str
    key: str
    status: str
    subject: str | None
    text: str
    chars: int
    file: str
    sent_at: str | None


class OutreachShow(BaseModel):
    job_id: int
    drafts: list[OutreachDraft]


class SentReport(BaseModel):
    job_id: int
    kind: str
    key: str
    sent_at: str
    dry_run: bool


def _today(now: str) -> date:
    return date.fromisoformat(now[:10])


def register(cli: click.Group) -> None:
    @agent_command(cli, "outreach due", output=DueReport)
    def due_cmd() -> Result:
        """Follow-ups that are due and top jobs worth asking about a referral."""
        paths, cfg = _prepare(True)
        today = _today(utc_now())
        with open_store(paths.db, dry_run=True) as conn:
            f = due_followups(conn, cfg.followups, today)
            r = referral_candidates(conn, cfg.fit.priority_threshold)
        user = [f"{c['question']} ({c['title']}) If so, add them in the tracker's Referral contact "
                "column or ask for a referral draft." for c in r]  # fmt: skip
        nxt = [
            f"Draft follow-ups with `jobhunt outreach draft <job> --kind followup` ({len(f)} due)."
        ]
        return Result(
            data=DueReport(followups=f, referrals=r),
            next_actions=nxt if f else [],
            user_actions=user,
        )

    @agent_command(cli, "outreach draft", output=OutreachTask, writes=True)
    @click.argument("job_id", type=int)
    @click.option("--kind", type=click.Choice(KINDS), required=True, help="Message kind.")
    @click.option(
        "--to", "recipient", default=None,
        help='Who it is for, in your words (e.g. "a former teammate on the data team").',
    )  # fmt: skip
    def draft_cmd(job_id: int, kind: str, recipient: str | None, dry_run: bool) -> Result:
        """Write a draft task for a referral request, coffee-chat ask or follow-up.

        You send the message yourself; jobhunt never sends anything. Follow-ups are allowed
        followups.after_days after applying, at most followups.max_per_application times.
        """
        paths, cfg = _prepare(dry_run)
        now = utc_now()
        inp = _inputs(paths, cfg, now)
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            job = _job(conn, job_id)
            application = None
            if kind == "followup":
                st = followup_state(conn, job_id, cfg.followups, _today(now))
                if st.sent >= cfg.followups.max_per_application:
                    raise ValidationFailed(
                        f"You've already sent {st.sent} follow-up(s) for job {job_id} "
                        f"(max {cfg.followups.max_per_application})."
                    )
                if st.due_on is None:
                    raise ValidationFailed(
                        f"Job {job_id} isn't awaiting a response (status Applied with a date).",
                        next_actions=[f"`jobhunt mark {job_id} Applied --applied-on YYYY-MM-DD`"],
                    )
                if not st.due:
                    raise ValidationFailed(
                        f"A follow-up for job {job_id} is due on {st.due_on.isoformat()}."
                    )
                application = {"applied_on": st.last.isoformat() if st.last else None,
                               "followups_sent": st.sent}  # fmt: skip
            sources = job_sources(conn, job.text, job.company, inp.profile, inp.answers)
            status, task_id, path = write_outreach_task(
                conn, paths.tasks, paths.results, job, kind, sources,  # type: ignore[arg-type]
                recipient=recipient, max_chars=cfg.drafts.outreach_max_chars,
                application=application, now=now, dry_run=dry_run,
            )  # fmt: skip
        result = paths.results / f"{task_id}.json"
        return Result(
            data=OutreachTask(job_id=job_id, kind=kind, status=status, task_id=task_id,
                              task_path=str(path), result_path=str(result)),
            next_actions=[f"Do the task in {path}, write {result}, then run `jobhunt ingest`."],
        )  # fmt: skip

    @agent_command(cli, "outreach show", output=OutreachShow)
    @click.argument("job_id", type=int)
    def show_cmd(job_id: int) -> Result:
        """Show a job's outreach drafts and the files to edit and send yourself."""
        paths, _ = _prepare(True)
        with open_store(paths.db, dry_run=True) as conn:
            _job(conn, job_id)
            rows = conn.execute(
                "SELECT * FROM drafts WHERE job_id=? AND kind != 'essay' ORDER BY id", (job_id,)
            ).fetchall()
        out = [
            OutreachDraft(kind=r["kind"], key=r["key"], status=r["status"], subject=r["prompt"],
                          text=r["text"], chars=len(r["text"]),
                          file=str(outreach_file(paths.work, job_id, r["key"])),
                          sent_at=r["sent_at"])
            for r in rows
        ]  # fmt: skip
        user = [
            f"Edit and send {d.file} yourself, then `jobhunt outreach mark-sent {job_id} "
            f"--kind {d.kind}`." for d in out if d.status == "draft"
        ]  # fmt: skip
        return Result(data=OutreachShow(job_id=job_id, drafts=out), user_actions=user)

    @agent_command(cli, "outreach mark-sent", output=SentReport, writes=True)
    @click.argument("job_id", type=int)
    @click.option("--kind", type=click.Choice(KINDS), required=True, help="Message kind.")
    def mark_sent_cmd(job_id: int, kind: str, dry_run: bool) -> Result:
        """Record that YOU sent a drafted message (jobhunt sends nothing). Counts follow-ups."""
        paths, _ = _prepare(dry_run)
        now = utc_now()
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            row = conn.execute(
                "SELECT id, key FROM drafts WHERE job_id=? AND kind=? AND status='draft' "
                "ORDER BY id DESC LIMIT 1",
                (job_id, kind),
            ).fetchone()
            if row is None:
                raise NotFound(f"No unsent {kind} draft for job {job_id}.")
            conn.execute(
                "UPDATE drafts SET status='sent', sent_at=?, updated_at=? WHERE id=?",
                (now, now, row["id"]),
            )
        return Result(data=SentReport(job_id=job_id, kind=kind, key=row["key"], sent_at=now,
                                      dry_run=dry_run))  # fmt: skip
