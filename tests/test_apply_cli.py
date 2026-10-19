"""End-to-end: inspect, coverage, packet, answers on every persona, offline."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
from pathlib import Path

import pytest
import yaml

from jobhunt.errors import ExitCode
from tests.conftest import PERSONAS, PERSONAS_DIR, REPO

BASE = REPO / "tests" / "fixtures" / "sources" / "base"
OVERLAY = REPO / "tests" / "fixtures" / "forms" / "overlay"
CLASSES = {"ready", "needs_answers", "essay", "manual"}


@pytest.fixture(scope="session")
def fx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The base source fixtures with the application-form overlay laid on top."""
    d = tmp_path_factory.mktemp("fx")
    shutil.copytree(BASE, d, dirs_exist_ok=True)
    shutil.copytree(OVERLAY, d, dirs_exist_ok=True)
    return d


def secrets_of(persona: str) -> list[str]:
    a = yaml.safe_load((PERSONAS_DIR / persona / "answers.yaml").read_text(encoding="utf-8"))
    ident = a.get("identity") or {}
    out = [ident.get(k) for k in ("email", "phone", "legal_last_name", "address_line1")]
    return [s for s in out if s]


def db(home: Path) -> sqlite3.Connection:
    c = sqlite3.connect(home / "jobhunt.db")
    c.row_factory = sqlite3.Row
    return c


@pytest.mark.parametrize("persona", PERSONAS)
def test_run_classifies_and_never_leaks(cli, persona_home, persona, fx, capsys):
    home = persona_home(persona)
    code, env, _ = cli("run", "--fixtures", str(fx), "--no-sync")
    assert code == ExitCode.OK, env
    with db(home) as c:
        ids = [str(r[0]) for r in c.execute("SELECT id FROM jobs WHERE live=1 ORDER BY id")]
    code, env, _ = cli("inspect", *ids, "--fixtures", str(fx))
    assert code == ExitCode.OK, env
    d = env["data"]
    assert d["inspected"] == len(ids)
    assert set(d["by_class"]) <= CLASSES
    for j in d["jobs"]:
        assert j["class"] in CLASSES
        if j["status"] != "ok":
            assert j["class"] == "manual"
    # every job classified has a form_specs row; manual ones say why
    with db(home) as c:
        for r in c.execute("SELECT class, status, error FROM form_specs"):
            assert r["class"] in CLASSES
            if r["class"] == "manual":
                assert r["error"]
    assert (home / "new_questions.yaml").exists()
    code, cov, _ = cli("coverage", "--fixtures", str(fx))
    assert code == ExitCode.OK, cov
    assert cov["data"]["ats_mix"]
    # sensitive values never reach an envelope
    blob = json.dumps([env, cov])
    for s in secrets_of(persona):
        assert s not in blob, s


@pytest.mark.parametrize("persona", PERSONAS)
def test_packet_for_every_live_job(cli, persona_home, persona, fx):
    home = persona_home(persona)
    cli("run", "--fixtures", str(fx), "--no-sync")
    with db(home) as c:
        ids = [r[0] for r in c.execute("SELECT id FROM jobs WHERE live=1 ORDER BY id LIMIT 6")]
    for jid in ids:
        code, env, _ = cli("packet", str(jid))
        assert code == ExitCode.OK, env
        p = Path(env["data"]["path"])
        assert p.exists()
        if os.name != "nt":  # Windows has no POSIX mode bits
            assert stat.S_IMODE(p.stat().st_mode) & 0o077 == 0  # owner-only
        page = p.read_text(encoding="utf-8")
        assert "<script>alert" not in page
        assert "jobhunt never submits" in page
        for s in secrets_of(persona):
            assert s not in json.dumps(env)


def test_simplify_job_gets_posting_text_and_a_band(cli, persona_home, fx):
    """Milestone 2 gap: a Simplify-only job has no text until its ATS posting is read."""
    home = persona_home("first-year-intern")  # doesn't watch the northwind board
    cli("run", "--fixtures", str(fx), "--no-sync", "--no-inspect")
    with db(home) as c:
        jid = c.execute("SELECT id FROM jobs WHERE ats_job_id='1099'").fetchone()[0]
        assert not c.execute("SELECT description FROM jobs WHERE id=?", (jid,)).fetchone()[0]
    code, env, _ = cli("inspect", str(jid), "--fixtures", str(fx))
    assert code == ExitCode.OK, env
    assert env["data"]["descriptions_stored"] == 1
    _, shown, _ = cli("show", str(jid))
    reqs = shown["data"]["requirements"]
    assert any("Python" in r["text"] for r in reqs)
    assert shown["data"]["fit"]["band"] is not None
    with db(home) as c:
        # a view, not a source: no presence row, so it can't keep the job live
        assert not c.execute(
            "SELECT 1 FROM source_jobs WHERE job_id=? AND source_id LIKE '%view%'", (jid,)
        ).fetchone()


def test_posting_view_never_keeps_a_closed_job_live(cli, persona_home, fx, tmp_path):
    home = persona_home("first-year-intern")
    cli("run", "--fixtures", str(fx), "--no-sync", "--no-inspect")
    with db(home) as c:
        jid = c.execute("SELECT id FROM jobs WHERE ats_job_id='1099'").fetchone()[0]
    cli("inspect", str(jid), "--fixtures", str(fx), "--no-match")
    later = tmp_path / "later"
    shutil.copytree(fx, later)
    listing = next(
        later.glob(
            "raw.githubusercontent.com/SimplifyJobs/Summer*/dev/.github/scripts/listings.json"
        )
    )
    rows = json.loads(listing.read_text(encoding="utf-8"))
    for r in rows:
        if r["url"].endswith("/jobs/1099"):
            r["active"] = False
    listing.write_text(json.dumps(rows), encoding="utf-8")
    cli("fetch", "--fixtures", str(later))
    with db(home) as c:
        live, closed = c.execute("SELECT live, closed_at FROM jobs WHERE id=?", (jid,)).fetchone()
        assert live == 0 and closed
        assert c.execute("SELECT 1 FROM posting_views WHERE job_id=?", (jid,)).fetchone()


def test_confirming_a_wording_moves_a_job_to_ready(cli, persona_home, fx):
    home = persona_home("intl-sponsorship")
    cli("run", "--fixtures", str(fx), "--no-sync", "--no-inspect")
    with db(home) as c:
        jid = c.execute("SELECT id FROM jobs WHERE ats_job_id='1004'").fetchone()[0]
    _, env, _ = cli("inspect", str(jid), "--fixtures", str(fx))
    job = env["data"]["jobs"][0]
    assert job["class"] == "needs_answers"
    _, qs, _ = cli("answers", "questions")
    by = {q["question"]: q for q in qs["data"]["questions"]}
    spons = next(q for q in by if "sponsor" in q)
    assert "[Company]" in spons and by[spons]["suggested_key"] == "work_auth.sponsorship"
    assert by[spons]["sensitive"]
    # not reused until confirmed; then the confirmation carries across companies
    code, _, _ = cli("answers", "confirm", spons, "work_auth.sponsorship")
    assert code == ExitCode.OK
    code, env, _ = cli(
        "answers",
        "add",
        "--question",
        "Do you have a valid driver's license?",
        "--answer",
        "Yes",
        "--option",
        "Yes",
    )
    assert code == ExitCode.OK, env
    _, env, _ = cli("inspect", str(jid), "--fixtures", str(fx))
    assert env["data"]["jobs"][0]["class"] == "ready", env["data"]["jobs"][0]


def test_inspect_dry_run_writes_nothing(cli, persona_home, fx):
    home = persona_home("senior-backend")
    cli("run", "--fixtures", str(fx), "--no-sync", "--no-inspect")
    before = (home / "jobhunt.db").read_bytes()
    code, env, _ = cli("inspect", "--top", "10", "--fixtures", str(fx), "--dry-run")
    assert code == ExitCode.OK and env["data"]["dry_run"]
    assert (home / "jobhunt.db").read_bytes() == before
    assert not (home / "new_questions.yaml").exists()


def test_inspect_unknown_job_is_not_found(cli, persona_home):
    persona_home("senior-backend")
    code, _env, _ = cli("inspect", "999999")
    assert code == ExitCode.NOT_FOUND


def test_unsupported_ats_is_manual_with_reason(cli, persona_home, fx):
    home = persona_home("first-year-intern")
    cli("run", "--fixtures", str(fx), "--no-sync", "--no-inspect")
    with db(home) as c:
        jid = c.execute("SELECT id FROM jobs WHERE ats='workday' LIMIT 1").fetchone()[0]
    _, env, _ = cli("inspect", str(jid), "--fixtures", str(fx))
    j = env["data"]["jobs"][0]
    assert j["class"] == "manual" and j["status"] == "unsupported"
    assert "packet" in j["reasons"][0]
