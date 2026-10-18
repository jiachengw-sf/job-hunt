"""Status changes outside the sheet: `jobhunt mark` (the user's choice) and agent moves."""

from __future__ import annotations

import sqlite3
from datetime import date

from jobhunt.errors import NotFound, ValidationFailed
from jobhunt.tracker.states import machine
from jobhunt.tracker.sync import _ensure_application, set_user_field


def mark(
    conn: sqlite3.Connection,
    job_id: int,
    status: str,
    *,
    reason: str | None,
    applied_on: str | None,
    today: date,
    now: str,
) -> dict[str, str | None]:
    """The user sets a status. Any status is allowed; Skipped needs a reason."""
    if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
        raise NotFound(f"No job {job_id}.", next_actions=["Run `jobhunt jobs` to list job ids."])
    state = machine().user_move(status, reason)
    app = _ensure_application(conn, job_id, now)
    before = app["status"]
    set_user_field(conn, job_id, "status", state.name, now, source="mark")
    if state.needs_reason:
        set_user_field(conn, job_id, "skip_reason", reason, now, source="mark")
    if applied_on:
        try:
            applied_on = date.fromisoformat(applied_on).isoformat()
        except ValueError as e:
            raise ValidationFailed(f"--applied-on {applied_on!r} is not YYYY-MM-DD.") from e
    if state.name == "Applied" and not (applied_on or app["applied_on"]):
        applied_on = today.isoformat()
    if applied_on:
        set_user_field(conn, job_id, "applied_on", applied_on, now, source="mark")
    after = conn.execute(
        "SELECT status, applied_on, skip_reason FROM applications WHERE job_id=?", (job_id,)
    ).fetchone()
    return {
        "from": before,
        "to": after["status"],
        "applied_on": after["applied_on"],
        "skip_reason": after["skip_reason"],
    }


def agent_advance(conn: sqlite3.Connection, job_id: int, status: str, now: str) -> bool:
    """The agent only moves a status forward (e.g. to Prefilled). False if not allowed."""
    app = _ensure_application(conn, job_id, now)
    if not machine().agent_can_move(app["status"], status):
        return False
    set_user_field(conn, job_id, "status", machine().get(status).name, now, source="agent")
    return True
