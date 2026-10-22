"""Human-readable views of your applications, generated from the store so they never drift:

  - ``app_view``: one application in plain words (local times, full names, no raw JSON); the
    CLI, the sheet's Applications tab, the dashboard and the Markdown all start from it
  - each application's folder ``README.md``

``refresh_views`` regenerates them after every command that changes an application.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, date, datetime, tzinfo
from typing import Any

from jobhunt.models.config import Config
from jobhunt.paths import Paths, secure_file
from jobhunt.tracker.applications import history
from jobhunt.tracker.timefmt import fmt_date, fmt_local, local_day, zone

log = logging.getLogger("jobhunt")

METHOD_TEXT = {
    "prefilled": "Pre-filled by jobhunt",
    "manual": "Applied yourself",
    "referral": "Referral",
    "other": "Other",
}
VIA_TEXT = {
    "prefill": "jobhunt saw the confirmation page",
    "cli": "you recorded it (`jobhunt applied` or `jobhunt mark`)",
    "chat": "you told your AI tool, which recorded it",
    "sheet": "you set Status in the spreadsheet",
}
SOURCE_TEXT = {"auto": "jobhunt", "user": "you", "sheet": "you, in the sheet"}
ACTIVE = ("Applied", "OA", "Interviewing")
WORK_MODE = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site", "on_site": "On-site"}


def _pay(pay_json: str | None) -> str:
    from jobhunt.tracker.sync import _pay_text

    return _pay_text(pay_json)


def where_to_check(rec: sqlite3.Row | dict[str, Any]) -> str:
    """'https://…' for a portal, else 'Email: …' or 'No updates expected'."""
    if rec["status_check_method"] == "portal" and rec["status_check_url"]:
        return rec["status_check_url"]
    if rec["status_check_method"] == "none":
        return "No status updates expected"
    who = rec["login_email"] or "the address you applied with"
    return f"Email: {who}"


def app_view(
    conn: sqlite3.Connection,
    rec: sqlite3.Row,
    cfg: Config,
    *,
    now: str,
    tz: tzinfo | None,
) -> dict[str, Any]:
    from jobhunt.drafts.outreach import followup_state

    app = conn.execute("SELECT * FROM applications WHERE job_id=?", (rec["job_id"],)).fetchone()
    today = local_day(now, tz)
    hist = history(conn, rec["job_id"])
    last_change = max([h["at"] for h in hist] + [rec["applied_at"]])
    days_applied = (today - local_day(rec["applied_at"], tz)).days
    days_quiet = (today - local_day(last_change, tz)).days
    status = app["status"] if app else "Applied"
    stale = status == "Applied" and days_quiet >= cfg.tracking.stale_after_days
    fu = followup_state(conn, rec["job_id"], cfg.followups, today)
    follow_up_on = app["follow_up_on"] if app and app["follow_up_on"] else None
    due_on = date.fromisoformat(follow_up_on) if follow_up_on else fu.due_on
    followup_due = (
        status == "Applied"
        and due_on is not None
        and today >= due_on
        and (bool(follow_up_on) or fu.due)
    )
    dates = [
        {"kind": r["kind"], "at": r["at"], "label": r["label"], "when": fmt_local(r["at"], tz)}
        for r in conn.execute(
            "SELECT kind, at, label FROM application_dates WHERE app_id=? ORDER BY at",
            (rec["id"],),
        )
    ]
    upcoming = [d for d in dates if d["kind"] == "interview" and d["at"] >= now]
    locations = json.loads(rec["locations_json"] or "[]")
    mode = WORK_MODE.get((rec["work_mode"] or "").lower(), rec["work_mode"] or "")
    where = "; ".join(locations) or "Not listed"
    if mode and mode.lower() not in where.lower():
        where += f" ({mode})"
    flags = []
    if stale:
        flags.append(f"likely no response ({days_quiet} days with no update)")
    if followup_due:
        flags.append(f"follow-up due {fmt_date(_day_ts(due_on), None)}")
    if upcoming:
        flags.append(f"interview {upcoming[0]['when']}")
    return {
        "app_id": rec["id"],
        "job_id": rec["job_id"],
        "company": rec["company"],
        "position": rec["title"],
        "team": rec["team"] or "",
        "location": where,
        "employment": ", ".join(x for x in (rec["employment_type"], rec["term"]) if x),
        "pay": _pay(rec["pay_json"]),
        "ats": rec["ats"] or "unknown",
        "ats_job_id": rec["ats_job_id"] or "",
        "posting_url": rec["posting_url"] or "",
        "applied_at": rec["applied_at"],
        "applied": fmt_local(rec["applied_at"], tz, approx=bool(rec["applied_at_approx"])),
        "applied_approx": bool(rec["applied_at_approx"]),
        "applied_day": local_day(rec["applied_at"], tz).isoformat(),
        "method": METHOD_TEXT[rec["method"]],
        "recorded_via": VIA_TEXT[rec["recorded_via"]],
        "status": status,
        "last_update_at": last_change,
        "last_update": fmt_local(last_change, tz),
        "days_since_applied": days_applied,
        "days_without_update": days_quiet,
        "stale": stale,
        "followup_due": followup_due,
        "followup_due_on": due_on.isoformat() if due_on else "",
        "followups_sent": fu.sent,
        "where_to_check": where_to_check(rec),
        "status_check_method": rec["status_check_method"],
        "status_check_url": rec["status_check_url"] or "",
        "status_check_note": rec["status_check_note"] or "",
        "login_email": rec["login_email"] or "",
        "confirmation_id": rec["confirmation_id"] or "",
        "referral_contact": (app["referral_contact"] if app else None) or "",
        "recruiter": " ".join(
            x
            for x in (
                rec["recruiter_name"],
                f"<{rec['recruiter_email']}>" if rec["recruiter_email"] else None,
            )
            if x
        ),
        "next_step": (app["next_step"] if app else None) or "",
        "outcome": (app["outcome"] if app else None) or "",
        "notes": (app["notes"] if app else None) or "",
        "resume_file": rec["resume_file"] or "",
        "resume_sha256": rec["resume_sha256"] or "",
        "submitted": json.loads(rec["submitted_json"] or "[]"),
        "folder": rec["folder"] or "",
        "has_screenshot": bool(rec["screenshot_path"]),
        "has_jd": bool(rec["jd_snapshot_path"]),
        "dates": dates,
        "upcoming_interviews": upcoming,
        "attention": flags,
        "history": [
            h | {"when": fmt_local(h["at"], tz), "who": SOURCE_TEXT[h["source"]]} for h in hist
        ],
    }


def _day_ts(d: date) -> str:
    return datetime(d.year, d.month, d.day, 12, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def all_views(
    conn: sqlite3.Connection, cfg: Config, *, now: str, tz: tzinfo | None
) -> list[dict[str, Any]]:
    """Every application, newest first."""
    rows = conn.execute("SELECT * FROM application_records ORDER BY applied_at DESC, id DESC")
    return [app_view(conn, r, cfg, now=now, tz=tz) for r in rows.fetchall()]


# --- the per-application folder README --------------------------------------------------------


def _line(label: str, value: Any) -> str | None:
    return f"- **{label}:** {value}" if value not in (None, "", []) else None


def folder_readme(v: dict[str, Any]) -> str:
    rows = [
        f"# {v['position']} — {v['company']}",
        "",
        f"**Status: {v['status']}** (last update {v['last_update']})",
        "",
        *(f"> {a[0].upper() + a[1:]}" for a in v["attention"]),
        "" if v["attention"] else None,
        "## What you applied for",
        "",
        _line("Company", v["company"]),
        _line("Position", v["position"]),
        _line("Team", v["team"]),
        _line("Location", v["location"]),
        _line("Type", v["employment"]),
        _line("Pay (as posted)", v["pay"]),
        _line("Posting", v["posting_url"]),
        _line(
            "Application system",
            f"{v['ats']}" + (f", job {v['ats_job_id']}" if v["ats_job_id"] else ""),
        ),
        "",
        "## When and how",
        "",
        _line("Applied", v["applied"]),
        _line("How", v["method"]),
        _line("Recorded because", v["recorded_via"]),
        _line("Referral", v["referral_contact"]),
        _line(
            "Resume",
            f"{v['resume_file']} (sha256 {v['resume_sha256'][:12]}…)"
            if v["resume_sha256"]
            else v["resume_file"],
        ),
        _line(
            "Sent",
            ", ".join(
                s["label"] + (" (sensitive)" if s["sensitive"] else "") for s in v["submitted"]
            )
            or None,
        ),
        _line("Confirmation ID", v["confirmation_id"]),
        "",
        "## Where to check status",
        "",
        _line("Check at", v["where_to_check"]),
        _line("How to check", v["status_check_note"]),
        _line("Sign-in email", v["login_email"]),
        "",
        "## Next",
        "",
        _line("Next step", v["next_step"]),
        _line(
            "Follow-up due",
            fmt_date(_day_ts(date.fromisoformat(v["followup_due_on"])), None)
            if v["followup_due_on"]
            else None,
        ),
        *(
            _line(d["kind"].capitalize(), d["when"] + (f" — {d['label']}" if d["label"] else ""))
            for d in v["dates"]
        ),
        _line("Recruiter", v["recruiter"]),
        _line("Outcome", v["outcome"]),
        _line("Notes", v["notes"].replace("\n", "; ")),
        "",
        "## Status timeline",
        "",
        *(
            f"- {h['when']}: {h['from'] or 'none'} → **{h['to']}** ({h['who']})"
            + (f" — {h['note']}" if h.get("note") else "")
            for h in v["history"]
        ),
        "",
        "## Files here",
        "",
        "- `job_description.md`: the posting as it was when you applied" if v["has_jd"] else None,
        "- `submitted_answers.md`: what was sent (jobhunt's code writes it; AI tools don't read "
        "it)",
        "- `confirmation.png`: the confirmation page" if v["has_screenshot"] else None,
        f"- `{v['resume_file']}`: a copy of the resume used" if v["resume_file"] else None,
        "",
        "_Generated by jobhunt from its store; edits here are overwritten. Change things with "
        "`jobhunt app update` or in the tracker sheet._",
        "",
    ]
    return "\n".join(r for r in rows if r is not None)


def write_folder(paths: Paths, v: dict[str, Any]) -> None:
    if not v["folder"]:
        return
    d = paths.applications / v["folder"]
    d.mkdir(parents=True, exist_ok=True)
    p = d / "README.md"
    p.write_text(folder_readme(v), encoding="utf-8")
    secure_file(p)


# --- regenerate everything --------------------------------------------------------------------


def refresh_views(
    conn: sqlite3.Connection, paths: Paths, cfg: Config, *, now: str | None = None
) -> list[str]:
    """Regenerate every human-readable view. Returns the files written. Never raises: a view
    that can't be written is logged, and the command that called this still succeeds."""
    from jobhunt.discovery.text import utc_now

    now = now or utc_now()
    written: list[str] = []
    try:
        tz = zone(cfg.tracking.timezone)
        views = all_views(conn, cfg, now=now, tz=tz)
        for v in views:
            write_folder(paths, v)
        written += [str(paths.applications / v["folder"] / "README.md") for v in views]
    except Exception as e:  # a view is a convenience; the store is the record
        log.warning("couldn't regenerate the application views: %s", e)
    return written
