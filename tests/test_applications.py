"""Application records: manual, `mark`, sheet and chat recording; local times."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from openpyxl import load_workbook

from jobhunt.errors import ExitCode, ValidationFailed
from jobhunt.tracker.timefmt import fmt_local, local_day, parse_when, zone
from tests.conftest import REPO

BASE = str(REPO / "tests" / "fixtures" / "sources" / "base")
WORKDAY = "https://harborlight.wd5.myworkdayjobs.com/en-US/careers/job/Charlotte-NC/Technology-Intern_R-10023"
LA = zone("America/Los_Angeles")


def db(home: Path) -> sqlite3.Connection:
    c = sqlite3.connect(home / "jobhunt.db")
    c.row_factory = sqlite3.Row
    return c


def setup(cli, persona_home, persona: str = "first-year-intern", **config) -> Path:
    home = persona_home(persona)
    cfg = {"tracking": {"timezone": "America/Los_Angeles"}} | config
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    code, env, _ = cli("run", "--fixtures", BASE, "--no-inspect", "--no-sync", "--no-notify")
    assert code == ExitCode.OK, env
    return home


def records(home: Path) -> list[sqlite3.Row]:
    with db(home) as c:
        return c.execute("SELECT * FROM application_records ORDER BY id").fetchall()


def job_id(home: Path, key_like: str) -> int:
    with db(home) as c:
        return c.execute(
            "SELECT id FROM jobs WHERE dedupe_key LIKE ? ORDER BY id", (key_like,)
        ).fetchone()[0]


# --- time ---------------------------------------------------------------------------------


def test_utc_round_trips_to_local_display_across_dst():
    # 2026-11-01 02:00 PDT -> 01:00 PST
    assert fmt_local("2026-10-22T04:42:00Z", LA) == "Wed, Oct 21, 2026 · 9:42 PM PDT"
    assert fmt_local("2026-11-02T05:42:00Z", LA) == "Sun, Nov 1, 2026 · 9:42 PM PST"
    assert fmt_local("2026-11-01T08:30:00Z", LA) == "Sun, Nov 1, 2026 · 1:30 AM PDT"
    assert fmt_local("2026-11-01T09:30:00Z", LA) == "Sun, Nov 1, 2026 · 1:30 AM PST"
    # what you type in local time comes back as the same local time on either side
    for local, stored in (
        ("2026-10-31 21:30", "2026-11-01T04:30:00Z"),
        ("2026-11-02 21:30", "2026-11-03T05:30:00Z"),
    ):
        utc, approx = parse_when(local, "2026-11-10T00:00:00Z", LA)
        assert (utc, approx) == (stored, False)
        assert fmt_local(utc, LA).endswith("9:30 PM " + ("PDT" if "10-31" in local else "PST"))
    assert local_day("2026-10-22T04:42:00Z", LA).isoformat() == "2026-10-21"
    assert fmt_local("2026-10-22T04:42:00Z", LA, approx=True) == (
        "Wed, Oct 21, 2026 (time approximate)"
    )


@pytest.mark.parametrize(
    ("text", "utc", "approx"),
    [
        ("now", "2026-10-22T04:42:00Z", False),
        ("today 3pm", "2026-10-21T22:00:00Z", False),
        ("this afternoon", "2026-10-21T22:00:00Z", False),
        ("yesterday 21:30", "2026-10-21T04:30:00Z", False),
        ("Oct 21 9:42pm", "2026-10-22T04:42:00Z", False),
        ("Oct 21, 2026 at 9:42 PM", "2026-10-22T04:42:00Z", False),
        ("2026-10-21 21:30", "2026-10-22T04:30:00Z", False),
        ("2026-10-21T21:30", "2026-10-22T04:30:00Z", False),
        ("2026-10-22T04:00:00Z", "2026-10-22T04:00:00Z", False),
        ("2 days ago", "2026-10-19T19:00:00Z", True),
        ("10/20", "2026-10-20T19:00:00Z", True),
        ("monday", "2026-10-19T19:00:00Z", True),
    ],
)
def test_natural_local_times(text, utc, approx):
    # "now" is Wed Oct 21, 9:42 PM in Los Angeles
    assert parse_when(text, "2026-10-22T04:42:00Z", LA) == (utc, approx)


@pytest.mark.parametrize("text", ["sometime", "25:00", "13pm", "tomorrow 3pm", "Oct 30 10am"])
def test_unreadable_or_future_times_are_refused(text):
    with pytest.raises(ValidationFailed):
        parse_when(text, "2026-10-22T04:42:00Z", LA)


def test_future_times_for_interviews():
    when = parse_when("Oct 30 10am", "2026-10-22T04:42:00Z", LA, allow_future=True)
    assert when == ("2026-10-30T17:00:00Z", False)


# --- recording ------------------------------------------------------------------------------


def test_manual_applied_for_a_workday_job(cli, persona_home):
    home = setup(cli, persona_home)
    code, env, _ = cli("applied", WORKDAY + "?utm_source=x", "--at", "yesterday 9:30pm",
                       "--login-email", "candidate@example.com")  # fmt: skip
    assert code == ExitCode.OK, env
    d = env["data"]
    assert d["created"] and d["company"] and d["status"] == "Applied"
    assert d["where_to_check"] == "https://harborlight.wd5.myworkdayjobs.com/careers/userHome"
    assert "Workday Candidate Home" in d["how_to_check"]
    assert d["applied"].endswith("9:30 PM PDT")
    (r,) = records(home)
    assert (r["method"], r["recorded_via"], r["status_check_method"]) == ("manual", "cli", "portal")
    assert r["login_email"] == "candidate@example.com" and r["resume_file"] == "resume.md"
    folder = home / "applications" / r["folder"]
    assert (folder / "job_description.md").exists() and (folder / "resume.md").exists()
    readme = (folder / "README.md").read_text(encoding="utf-8")
    assert "userHome" in readme and "Status timeline" in readme
    with db(home) as c:
        app = c.execute("SELECT status, applied_on FROM applications WHERE job_id=?",
                        (r["job_id"],)).fetchone()  # fmt: skip
    assert app["status"] == "Applied" and app["applied_on"]

    code, again, _ = cli("applied", str(r["job_id"]))  # re-run: no second record
    assert code == ExitCode.OK and not again["data"]["created"]
    assert again["data"]["app_id"] == r["id"] and len(records(home)) == 1


def test_applied_unknown_url_points_to_add(cli, persona_home):
    setup(cli, persona_home)
    code, env, _ = cli("applied", "https://careers.example.com/jobs/42")
    assert code == ExitCode.NOT_FOUND
    assert "jobhunt add https://careers.example.com/jobs/42" in env["hints"]["next_actions"][0]


def test_applied_dry_run_writes_nothing(cli, persona_home):
    home = setup(cli, persona_home)
    code, env, _ = cli("applied", WORKDAY, "--dry-run")
    assert code == ExitCode.OK and env["data"]["created"] and env["data"]["dry_run"]
    assert records(home) == [] and not (home / "applications").exists()


def test_chat_and_portal_override(cli, persona_home):
    home = setup(cli, persona_home)
    gh = job_id(home, "greenhouse:%")
    code, env, _ = cli("applied", str(gh), "--via", "chat", "--at", "today 2pm", "--portal",
                       "https://careers.example.com/me", "--check-note", "Sign in with Gmail",
                       "--referral", "A. Friend", "--note", "applied from my phone")  # fmt: skip
    assert code == ExitCode.OK, env
    (r,) = records(home)
    assert r["recorded_via"] == "chat" and r["method"] == "referral"
    assert r["status_check_url"] == "https://careers.example.com/me"
    assert r["status_check_note"] == "Sign in with Gmail"
    with db(home) as c:
        app = c.execute("SELECT referral_contact, notes FROM applications WHERE job_id=?",
                        (gh,)).fetchone()  # fmt: skip
    assert tuple(app) == ("A. Friend", "applied from my phone")
    cli("applied", str(gh), "--via", "chat")
    assert len(records(home)) == 1


def test_mark_applied_records_once(cli, persona_home):
    home = setup(cli, persona_home)
    jid = job_id(home, "greenhouse:%")
    code, env, _ = cli("mark", str(jid), "Applied", "--applied-on", "2026-10-01")
    assert code == ExitCode.OK, env
    (r,) = records(home)
    assert r["applied_at_approx"] and r["applied_at"].startswith("2026-10-01")
    cli("mark", str(jid), "OA")
    cli("mark", str(jid), "Applied")
    assert len(records(home)) == 1
    code, env, _ = cli("mark", str(job_id(home, "lever:%")), "Skipped", "--reason", "pay")
    assert code == ExitCode.OK and len(records(home)) == 1  # skipping isn't applying


def _set_sheet_status(xlsx: Path, job: int, status: str) -> None:
    wb = load_workbook(xlsx)
    ws = wb["Jobs"]
    header = [c.value for c in ws[1]]
    id_c, st_c = header.index("jobhunt ID") + 1, header.index("Status") + 1
    for row in range(2, ws.max_row + 1):
        if str(ws.cell(row, id_c).value) == str(job):
            ws.cell(row, st_c, status)
    wb.save(xlsx)


def test_sheet_status_applied_records_once(cli, persona_home):
    home = setup(cli, persona_home)
    code, env, _ = cli("sync")
    assert code == ExitCode.OK, env
    jid = job_id(home, "greenhouse:%")
    _set_sheet_status(home / "tracker.xlsx", jid, "Applied")
    code, env, _ = cli("sync")
    assert code == ExitCode.OK and env["data"]["applications_recorded"] == 1, env
    (r,) = records(home)
    assert r["recorded_via"] == "sheet" and r["applied_at_approx"] == 1
    _set_sheet_status(home / "tracker.xlsx", jid, "Interviewing")
    code, env, _ = cli("sync")
    assert env["data"]["applications_recorded"] == 0 and len(records(home)) == 1
    with db(home) as c:
        hist = [
            (e["source_id"], json.loads(e["details_json"])["to"])
            for e in c.execute(
                "SELECT * FROM events WHERE job_id=? AND kind='status_changed' ORDER BY id", (jid,)
            )
        ]
    assert hist == [("tracker", "Applied"), ("tracker", "Interviewing")]


def test_volume_guard_warning_on_record(cli, persona_home):
    home = setup(cli, persona_home, volume_guard={"max_applications_per_company": 1})
    with db(home) as c:
        rows = c.execute(
            "SELECT company, group_concat(id) AS ids FROM jobs GROUP BY company "
            "HAVING count(*) >= 2"
        ).fetchall()
    if not rows:
        pytest.skip("fixtures have no company with two jobs")
    a, b = rows[0]["ids"].split(",")[:2]
    _, first, _ = cli("applied", a)
    assert not first["data"]["warnings"]
    _, second, _ = cli("applied", b)
    assert second["data"]["created"]
    assert any("volume guard" in w for w in second["data"]["warnings"])
    assert any("volume guard" in w for w in second["hints"]["user_actions"])
