"""Application tracking commands: applied, apps, app show / status / update, dashboard."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Any

import click
from pydantic import BaseModel, Field

from jobhunt.cli.discovery import _prepare
from jobhunt.cli.framework import Result, agent_command
from jobhunt.discovery.text import utc_now
from jobhunt.errors import ValidationFailed
from jobhunt.locking import file_lock
from jobhunt.models import Config
from jobhunt.paths import Paths
from jobhunt.store import open_store
from jobhunt.tracker.applications import (
    CHECK_METHODS,
    METHODS,
    get_record,
    record_application,
    resolve_job,
    submitted_essays,
)
from jobhunt.tracker.timefmt import parse_when, zone
from jobhunt.tracker.views import app_view, refresh_views


def _lock(paths: Paths, dry_run: bool) -> Any:
    return nullcontext() if dry_run else file_lock(paths.lock)


def _resume(paths: Paths, given: str | None) -> Path | None:
    from jobhunt.cli.onboarding import default_resume

    if not given:
        return default_resume(paths)
    p = Path(given).expanduser()
    if not p.is_absolute() and not p.exists():
        p = paths.resume_dir / given
    if not p.exists():
        raise ValidationFailed(
            f"No resume file {given!r}.",
            next_actions=["Pass a file path, or a file name inside ~/.jobhunt/resume/."],
        )
    return p


class Recorded(BaseModel):
    """What was recorded, in plain words (no answer values)."""

    dry_run: bool
    created: bool = Field(description="False when the job already had a record (nothing added).")
    app_id: int
    job_id: int
    company: str
    position: str
    applied: str = Field(description="Local time, e.g. 'Wed, Oct 21, 2026 · 9:42 PM PDT'.")
    method: str
    status: str
    where_to_check: str
    how_to_check: str
    folder: str | None = None
    warnings: list[str] = Field(default_factory=list)


def recorded(conn: Any, out: Any, cfg: Config, *, now: str, dry_run: bool) -> Recorded:
    tz = zone(cfg.tracking.timezone)
    rec = get_record(conn, out.job_id)
    v = app_view(conn, rec, cfg, now=now, tz=tz)
    return Recorded(
        dry_run=dry_run,
        created=out.created,
        app_id=out.app_id,
        job_id=out.job_id,
        company=v["company"],
        position=v["position"],
        applied=v["applied"],
        method=v["method"],
        status=v["status"],
        where_to_check=v["where_to_check"],
        how_to_check=v["status_check_note"],
        folder=v["folder"] or None,
        warnings=out.warnings,
    )


def _render_recorded(env: Any) -> str:
    from jobhunt.envelope import render_human_default

    d = env.data
    if not isinstance(d, dict) or "app_id" not in d:
        return render_human_default(env)
    verb = "Recorded" if d["created"] else "Already recorded"
    lines = [
        f"{verb}: {d['position']} at {d['company']} (application {d['app_id']})",
        f"  Applied    {d['applied']} · {d['method']}",
        f"  Status     {d['status']}",
        f"  Check at   {d['where_to_check']}",
    ]
    if d["how_to_check"]:
        lines.append(f"  How        {d['how_to_check']}")
    lines += [f"  ! {w}" for w in d["warnings"]]
    if d["dry_run"]:
        lines.append("(dry run: nothing was saved)")
    return "\n".join(lines)


def register(cli: click.Group) -> None:
    @agent_command(cli, "applied", output=Recorded, writes=True, render=_render_recorded)
    @click.argument("job")
    @click.option(
        "--at",
        "at",
        default="now",
        show_default=True,
        help='When you applied, in your local time: "today 3pm", "yesterday 21:30", '
        '"Oct 21 9:42pm", "2026-10-21 21:30", "2 days ago".',
    )
    @click.option(
        "--method",
        type=click.Choice(METHODS),
        default=None,
        help="How you applied (default manual; referral when --referral is given).",
    )
    @click.option("--referral", default=None, help="Who referred you.")
    @click.option("--portal", default=None, help="URL where you can check the status.")
    @click.option(
        "--check-method",
        type=click.Choice(CHECK_METHODS),
        default=None,
        help="portal, email or none (default from the ATS; portal when --portal is given).",
    )
    @click.option("--login-email", default=None, help="Email the account or application used.")
    @click.option("--check-note", default=None, help='How to check, e.g. "sign in with Gmail".')
    @click.option("--resume", default=None, help="Resume file used (default: your default resume).")
    @click.option("--note", default=None, help="A note, added to the job's Notes.")
    @click.option(
        "--via",
        type=click.Choice(["cli", "chat"]),
        default="cli",
        show_default=True,
        help="chat when an AI tool records what you told it.",
    )
    def applied_cmd(
        job: str,
        at: str,
        method: str | None,
        referral: str | None,
        portal: str | None,
        check_method: str | None,
        login_email: str | None,
        check_note: str | None,
        resume: str | None,
        note: str | None,
        via: str,
        dry_run: bool,
    ) -> Result:
        """Record that you applied to a job, wherever you applied (Workday, email, anywhere).

        JOB is a job id, job key or posting URL; add an unknown URL with `jobhunt add` first.
        Records once per job (re-running shows the existing record). Sets the status to Applied,
        snapshots the job description, and fills in where to check the status from the ATS.
        """
        paths, cfg = _prepare(dry_run)
        now = utc_now()
        tz = zone(cfg.tracking.timezone)
        applied_at, approx = parse_when(at, now, tz)
        if portal and not portal.startswith(("https://", "http://")):
            raise ValidationFailed("--portal must be an http(s) URL.")
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            job_id = resolve_job(conn, job)
            out = record_application(
                conn,
                job_id,
                now=now,
                applied_at=applied_at,
                approx=approx,
                method=method or ("referral" if referral else "manual"),
                via=via,
                tz=tz,
                paths=None if dry_run else paths,
                window_days=cfg.volume_guard.window_days,
                max_per_company=cfg.volume_guard.max_applications_per_company,
                resume=_resume(paths, resume),
                submitted=submitted_essays(conn, job_id),
                portal_url=portal,
                login_email=login_email,
                check_note=check_note,
                referral=referral,
                note=note,
            )
            if check_method and out.created:
                conn.execute(
                    "UPDATE application_records SET status_check_method=? WHERE id=?",
                    (check_method, out.app_id),
                )
            data = recorded(conn, out, cfg, now=now, dry_run=dry_run)
            if not dry_run:
                refresh_views(conn, paths, cfg, now=now)
        return Result(
            data=data,
            user_actions=data.warnings,
            next_actions=["Run `jobhunt sync` to update the tracker."] if out.created else [],
        )
