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

    _register_views(cli)


# --- listing, showing, changing -------------------------------------------------------------


class AppRow(BaseModel):
    app_id: int
    job_id: int
    company: str
    position: str
    location: str
    status: str
    applied: str
    last_update: str
    days_since_applied: int
    where_to_check: str
    next_step: str = ""
    followup_due_on: str = ""
    attention: list[str] = Field(default_factory=list)


class AppsList(BaseModel):
    summary: dict[str, Any] = Field(
        description="total, by_status, this_week, this_month, interviews, interview_rate, "
        "waiting, needs_attention, upcoming_interviews"
    )
    apps: list[AppRow]


def _row(v: dict[str, Any]) -> AppRow:
    return AppRow(**{k: v[k] for k in AppRow.model_fields})


def _brief_summary(s: dict[str, Any]) -> dict[str, Any]:
    from jobhunt.tracker.views import attention_count

    return {
        "total": s["total"],
        "by_status": s["by_status"],
        "this_week": s["this_week"],
        "this_month": s["this_month"],
        "interviews": s["interviews"],
        "interview_rate": s["interview_rate"],
        "waiting": s["waiting"],
        "needs_attention": attention_count(s),
        "followups_due": len(s["followups_due"]),
        "likely_no_response": len(s["stale"]),
        "upcoming_interviews": [
            f"{v['company']} — {v['position']}: {v['interview']['when']}"
            for v in s["upcoming_interviews"]
        ],
    }


def _table(rows: list[list[str]]) -> list[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows]


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _render_apps(env: Any) -> str:
    from jobhunt.envelope import render_human_default

    d = env.data
    if not isinstance(d, dict) or "apps" not in d:
        return render_human_default(env)
    s = d["summary"]
    rate = f"{s['interview_rate']:.0%}" if s["interview_rate"] is not None else "n/a"
    statuses = ", ".join(f"{k} {v}" for k, v in s["by_status"].items()) or "none yet"
    lines = [
        f"{s['total']} application(s): {statuses}",
        f"This week {s['this_week']} · this month {s['this_month']} · interview rate {rate} · "
        f"waiting to hear {s['waiting']} · needs attention {s['needs_attention']}",
    ]
    lines += [f"Upcoming interview: {u}" for u in s["upcoming_interviews"]]
    if d["apps"]:
        rows = [["ID", "Company", "Position", "Applied", "Status", "Days", "Where to check"]]
        for a in d["apps"]:
            applied = a["applied"].split(" · ")[0].replace(" (time approximate)", "~")
            rows.append([
                str(a["app_id"]), _cut(a["company"], 22), _cut(a["position"], 34), applied,
                a["status"], str(a["days_since_applied"]), _cut(a["where_to_check"], 48),
            ])  # fmt: skip
        lines += ["", *_table(rows)]
        flagged = [a for a in d["apps"] if a["attention"]]
        if flagged:
            lines += ["", "Needs attention:"]
            lines += [
                f"  {a['app_id']}: {a['company']} — {'; '.join(a['attention'])}" for a in flagged
            ]
    if env.page and env.page.next_cursor:
        lines.append(f"\nMore: --cursor {env.page.next_cursor}")
    return "\n".join(lines)


def _render_show(env: Any) -> str:
    from jobhunt.envelope import render_human_default

    v = env.data
    if not isinstance(v, dict) or "history" not in v:
        return render_human_default(env)
    rows = [
        ("Status", v["status"] + (f" — {'; '.join(v['attention'])}" if v["attention"] else "")),
        ("Applied", f"{v['applied']} · {v['method']}"),
        ("Location", v["location"]),
        ("Type", v["employment"]),
        ("Pay", v["pay"]),
        ("Check at", v["where_to_check"]),
        ("How", v["status_check_note"]),
        ("Sign-in email", v["login_email"]),
        ("Confirmation", v["confirmation_id"]),
        ("Referral", v["referral_contact"]),
        ("Recruiter", v["recruiter"]),
        ("Next step", v["next_step"]),
        ("Follow-up due", v["followup_due_on"]),
        *((d["kind"].capitalize(), d["when"] + (f" — {d['label']}" if d["label"] else ""))
          for d in v["dates"]),
        ("Resume", v["resume_file"]),
        ("Posting", v["posting_url"]),
        ("Folder", v["folder_path"]),
        ("Notes", v["notes"].replace("\n", "; ")),
    ]  # fmt: skip
    lines = [f"{v['position']} — {v['company']} (application {v['app_id']})", ""]
    lines += [f"  {k:<14}{val}" for k, val in rows if val]
    lines += ["", "Timeline:"]
    lines += [
        f"  {h['when']:<34}{(h['from'] or 'none') + ' → ' + h['to']:<28}{h['who']}"
        + (f" — {h['note']}" if h.get("note") else "")
        for h in v["history"]
    ]
    return "\n".join(lines)


def _views(paths: Paths, cfg: Config, conn: Any, now: str) -> list[dict[str, Any]]:
    from jobhunt.tracker.views import all_views

    return all_views(conn, cfg, now=now, tz=zone(cfg.tracking.timezone))


def _show(conn: Any, paths: Paths, cfg: Config, rec: Any, now: str) -> dict[str, Any]:
    v = app_view(conn, rec, cfg, now=now, tz=zone(cfg.tracking.timezone))
    v["folder_path"] = str(paths.applications / v["folder"]) if v["folder"] else ""
    return v


def _render_status(env: Any) -> str:
    from jobhunt.envelope import render_human_default

    d = env.data
    if not isinstance(d, dict) or "change" not in d:
        return render_human_default(env)
    c = d["change"]
    lines = [
        f"{d['company']} — {d['position']}: {c['from']} → {c['to']} ({c['at']})"
        + (" [dry run]" if d["dry_run"] else ""),
        "",
        "Timeline:",
    ]
    lines += [
        f"  {h['when']:<34}{(h['from'] or 'none') + ' → ' + h['to']:<28}{h['who']}"
        + (f" — {h['note']}" if h.get("note") else "")
        for h in d["history"]
    ]
    return "\n".join(lines)


class StatusChange(BaseModel):
    app_id: int
    dry_run: bool
    company: str
    position: str
    change: dict[str, str | None] = Field(description="from, to, at (local), note")
    history: list[dict[str, Any]]


def _register_views(cli: click.Group) -> None:
    from jobhunt.cli.framework import default_subcommand
    from jobhunt.envelope import paginate
    from jobhunt.tracker.applications import append_note, resolve_app
    from jobhunt.tracker.states import machine
    from jobhunt.tracker.sync import set_user_field

    @agent_command(cli, "apps list", output=AppsList, render=_render_apps)
    @click.option("--status", "statuses", multiple=True, help="Only these statuses (repeatable).")
    @click.option(
        "--attention", is_flag=True, help="Only follow-ups due, stale and upcoming interviews."
    )
    @click.option("--limit", type=int, default=50, help="Applications per page.")
    @click.option("--cursor", default=None, help="Page cursor from a previous call.")
    def apps_cmd(statuses: tuple[str, ...], attention: bool, limit: int, cursor: str | None
                 ) -> Result:  # fmt: skip
        """Your applications, newest first, with a summary. Bare `jobhunt apps` runs this."""
        from jobhunt.tracker.views import summary

        paths, cfg = _prepare(True)
        now = utc_now()
        with open_store(paths.db, dry_run=True) as conn:
            views = _views(paths, cfg, conn, now)
        s = summary(views, now=now, tz=zone(cfg.tracking.timezone))
        wanted = {machine().get(x).name for x in statuses}
        rows = [
            v for v in views
            if (not wanted or v["status"] in wanted) and (not attention or v["attention"])
        ]  # fmt: skip
        chunk, page = paginate(rows, cursor, limit)
        return Result(
            data=AppsList(summary=_brief_summary(s), apps=[_row(v) for v in chunk]),
            page=page,
            next_actions=[
                "`jobhunt app show <id>` for one application; `jobhunt dashboard` for the page."
            ]
            if views
            else ["No applications yet. Record one with `jobhunt applied <job id or URL>`."],
        )

    default_subcommand(cli, "apps", "list")

    @agent_command(cli, "app show", output=None, render=_render_show)
    @click.argument("app")
    def app_show_cmd(app: str) -> Result:
        """Everything about one application: what, when, how, where to check, and its timeline.

        APP is an application id (from `jobhunt apps`), or `job:<job id>`, a job key or a
        posting URL. Submitted answers are listed by label only.
        """
        paths, cfg = _prepare(True)
        with open_store(paths.db, dry_run=True) as conn:
            v = _show(conn, paths, cfg, resolve_app(conn, app), utc_now())
        return Result(data=v)

    @agent_command(cli, "app status", output=StatusChange, writes=True, render=_render_status)
    @click.argument("app")
    @click.argument("status")
    @click.option("--note", default=None, help="Why, or what happened (kept in the timeline).")
    @click.option("--at", "at", default="now", show_default=True, help="When, in local time.")
    @click.option("--reason", default=None, help="Why you're skipping (Skipped only).")
    def app_status_cmd(
        app: str, status: str, note: str | None, at: str, reason: str | None, dry_run: bool
    ) -> Result:
        """Change an application's status as you (any status from states.yml).

        Every change is added to its timeline; history is never rewritten.
        """
        paths, cfg = _prepare(dry_run)
        now = utc_now()
        tz = zone(cfg.tracking.timezone)
        when, _ = parse_when(at, now, tz)
        state = machine().user_move(status, reason)
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            rec = resolve_app(conn, app)
            before = conn.execute(
                "SELECT status FROM applications WHERE job_id=?", (rec["job_id"],)
            ).fetchone()[0]
            set_user_field(conn, rec["job_id"], "status", state.name, when, source="user",
                           note=note)  # fmt: skip
            if state.needs_reason:
                set_user_field(conn, rec["job_id"], "skip_reason", reason, when, source="user")
            if note and before == state.name:
                append_note(conn, rec["job_id"], note, when)
            v = app_view(conn, rec, cfg, now=now, tz=tz)
            if not dry_run:
                refresh_views(conn, paths, cfg, now=now)
        from jobhunt.tracker.timefmt import fmt_local

        return Result(
            data=StatusChange(
                app_id=rec["id"],
                dry_run=dry_run,
                company=v["company"],
                position=v["position"],
                change={"from": before, "to": state.name, "at": fmt_local(when, tz), "note": note},
                history=[
                    {k: h[k] for k in ("when", "from", "to", "who", "note")} for h in v["history"]
                ],
            ),
            next_actions=["Run `jobhunt sync` to update the tracker."],
        )

    @agent_command(cli, "app update", output=None, writes=True, render=_render_show)
    @click.argument("app")
    @click.option("--portal", default=None, help="URL where you check the status.")
    @click.option("--check-method", type=click.Choice(CHECK_METHODS), default=None)
    @click.option("--login-email", default=None, help="Email the account or application used.")
    @click.option("--check-note", default=None, help="How to check, in your words.")
    @click.option("--method", type=click.Choice(METHODS), default=None, help="How you applied.")
    @click.option(
        "--interview", "interviews", multiple=True, help='An interview time, e.g. "Oct 30 10am".'
    )
    @click.option("--deadline", "deadlines", multiple=True, help="A deadline (e.g. an OA's).")
    @click.option("--label", default=None, help="Label for the --interview/--deadline given.")
    @click.option("--recruiter-name", default=None)
    @click.option("--recruiter-email", default=None)
    @click.option("--next-step", default=None)
    @click.option("--follow-up-on", default=None, help="Date to follow up (YYYY-MM-DD).")
    @click.option("--outcome", default=None)
    @click.option("--note", default=None, help="Added to the notes.")
    def app_update_cmd(app: str, portal: str | None, check_method: str | None,
                       login_email: str | None, check_note: str | None, method: str | None,
                       interviews: tuple[str, ...], deadlines: tuple[str, ...],
                       label: str | None, recruiter_name: str | None,
                       recruiter_email: str | None, next_step: str | None,
                       follow_up_on: str | None, outcome: str | None, note: str | None,
                       dry_run: bool) -> Result:  # fmt: skip
        """Update an application: where and how to check it, interviews, contacts, next step.

        Never stores a password or a one-time code.
        """
        from datetime import date

        paths, cfg = _prepare(dry_run)
        now = utc_now()
        tz = zone(cfg.tracking.timezone)
        if portal and not portal.startswith(("https://", "http://")):
            raise ValidationFailed("--portal must be an http(s) URL.")
        if follow_up_on:
            try:
                follow_up_on = date.fromisoformat(follow_up_on).isoformat()
            except ValueError as e:
                raise ValidationFailed("--follow-up-on must be YYYY-MM-DD.") from e
        dates = [("interview", parse_when(x, now, tz, allow_future=True)[0]) for x in interviews]
        dates += [("deadline", parse_when(x, now, tz, allow_future=True)[0]) for x in deadlines]
        rec_fields = {
            "status_check_url": portal,
            "status_check_method": check_method or ("portal" if portal else None),
            "login_email": login_email,
            "status_check_note": check_note,
            "method": method,
            "recruiter_name": recruiter_name,
            "recruiter_email": recruiter_email,
        }
        rec_fields = {k: v for k, v in rec_fields.items() if v is not None}
        user_fields = {"next_step": next_step, "follow_up_on": follow_up_on, "outcome": outcome}
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            rec = resolve_app(conn, app)
            if rec_fields:
                sets = ", ".join(f"{k}=?" for k in rec_fields)
                conn.execute(
                    f"UPDATE application_records SET {sets}, updated_at=? WHERE id=?",
                    (*rec_fields.values(), now, rec["id"]),
                )
            for kind, at in dates:
                conn.execute(
                    "INSERT OR IGNORE INTO application_dates (app_id, kind, at, label) "
                    "VALUES (?,?,?,?)",
                    (rec["id"], kind, at, label),
                )
            for k, val in user_fields.items():
                if val is not None:
                    set_user_field(conn, rec["job_id"], k, val or None, now, source="user")
            if note:
                append_note(conn, rec["job_id"], note, now)
            rec = resolve_app(conn, str(rec["id"]))
            v = _show(conn, paths, cfg, rec, now)
            if not dry_run:
                refresh_views(conn, paths, cfg, now=now)
        return Result(data=v, next_actions=["Run `jobhunt sync` to update the tracker."])

    @agent_command(cli, "apps export", output=None, writes=True)
    @click.option("--csv", "fmt", flag_value="csv", default=True, help="CSV (the default).")
    @click.option("--xlsx", "fmt", flag_value="xlsx", help="An Excel workbook.")
    @click.option(
        "--to",
        "dest",
        type=click.Path(dir_okay=False, path_type=Path),
        default=None,
        help="File to write (default: applications.csv / .xlsx in your jobhunt folder).",
    )
    def apps_export_cmd(fmt: str, dest: Path | None, dry_run: bool) -> Result:
        """Export every application with readable column names (CSV or XLSX)."""
        from jobhunt.tracker.export import export_rows, write_csv, write_xlsx

        paths, cfg = _prepare(dry_run)
        now = utc_now()
        with open_store(paths.db, dry_run=True) as conn:
            rows = export_rows(_views(paths, cfg, conn, now))
        dest = dest or paths.home / f"applications.{fmt}"
        if not dry_run:
            (write_xlsx if fmt == "xlsx" else write_csv)(dest, rows)
        return Result(data={"file": str(dest), "rows": len(rows) - 1, "dry_run": dry_run})
