"""`jobhunt prefill` end to end on fixtures: batch, Prefilled, handoff, Applied, never submit."""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from datetime import date
from pathlib import Path

import pytest
import yaml

from jobhunt.cli.apply import form_inputs
from jobhunt.cli.prefill import prepare_job
from jobhunt.config import load_config
from jobhunt.errors import ExitCode
from jobhunt.paths import get_paths
from jobhunt.prefill import driver
from jobhunt.prefill.adapters import adapter_for
from jobhunt.prefill.fixtures import FixtureRouter
from jobhunt.store import open_store
from tests.conftest import PERSONAS, REPO
from tests.test_apply_cli import secrets_of

BASE = REPO / "tests" / "fixtures" / "sources" / "base"
RESUMES = REPO / "tests" / "fixtures" / "resumes"
SUBMITS = ("/jobs/", "/apply", "op=ApiSubmit")


@pytest.fixture(scope="session")
def fx_all(fx_pages, tmp_path_factory) -> Path:
    """Sources, form specs and browser pages in one fixture directory."""
    d = tmp_path_factory.mktemp("fx_all")
    shutil.copytree(BASE, d, dirs_exist_ok=True)
    shutil.copytree(fx_pages, d, dirs_exist_ok=True)
    return d


def db(home: Path) -> sqlite3.Connection:
    c = sqlite3.connect(home / "jobhunt.db")
    c.row_factory = sqlite3.Row
    return c


def setup_persona(cli, persona_home, persona: str, fx: Path) -> Path:
    home = persona_home(persona)
    shutil.copy(RESUMES / f"{persona}.pdf", home / "resume" / "resume.pdf")
    prof = yaml.safe_load((home / "profile.yaml").read_text())
    prof["resumes"] = [{"file": "resume.pdf", "default": True}]
    (home / "profile.yaml").write_text(yaml.safe_dump(prof, sort_keys=False))
    code, env, _ = cli("run", "--fixtures", str(fx), "--no-sync", "--no-inspect")
    assert code == ExitCode.OK, env
    with db(home) as c:
        ids = [str(r[0]) for r in c.execute("SELECT id FROM jobs WHERE live=1")]
    code, env, _ = cli("inspect", *ids, "--fixtures", str(fx), "--no-match")
    assert code == ExitCode.OK, env
    return home


def status_of(home: Path, job_id: int) -> tuple[str | None, str | None]:
    with db(home) as c:
        r = c.execute("SELECT status, applied_on FROM applications WHERE job_id=?", (job_id,))
        row = r.fetchone()
    return (row["status"], row["applied_on"]) if row else (None, None)


@pytest.mark.parametrize("persona", PERSONAS)
def test_dry_run_builds_and_validates_without_a_browser(cli, persona_home, persona, fx_all):
    home = setup_persona(cli, persona_home, persona, fx_all)
    code, env, _ = cli("prefill", "--dry-run", "--batch", "20")
    assert code == ExitCode.OK, env
    d = env["data"]
    assert d["dry_run"] and not d["running"]
    queued = [j for j in d["jobs"] if j["status"] == "queued"]
    assert queued, d
    assert {j["ats"] for j in queued} <= {"greenhouse", "lever", "ashby"}
    assert not (home / "work" / "prefill" / "state.json").exists()
    assert not list((home / "work" / "prefill").rglob("answers.json"))
    for s in secrets_of(persona):
        assert s not in json.dumps(env)


def test_not_ready_and_unknown_jobs(cli, persona_home, fx_all):
    home = setup_persona(cli, persona_home, "intl-sponsorship", fx_all)
    with db(home) as c:
        essay = c.execute("SELECT job_id FROM form_specs WHERE class='essay' LIMIT 1").fetchone()
    code, env, _ = cli("prefill", str(essay[0]), "--dry-run")
    assert code == ExitCode.OK
    j = env["data"]["jobs"][0]
    assert j["status"] == "skipped" and "essay" in j["reason"]
    code, env, _ = cli("prefill", "999999", "--dry-run")
    assert code == ExitCode.NOT_FOUND


def test_fill_handoff_and_confirmation_in_process(cli, persona_home, fx_all, browser_context):
    """Fill a batch, then the human submits one tab and closes another."""
    home = setup_persona(cli, persona_home, "intl-sponsorship", fx_all)
    paths = get_paths()
    _, env, _ = cli("prefill", "--dry-run")
    cfg = load_config(paths)
    inp = form_inputs(paths, cfg, "2026-10-18T12:00:00Z")
    ids = [j["job_id"] for j in env["data"]["jobs"] if j["status"] == "queued"]
    with open_store(paths.db, dry_run=True) as conn:
        batch = [prepare_job(conn, i, inp, paths, "now", dry_run=False)[1] for i in ids]
    router = FixtureRouter(fx_all)
    router.install(browser_context)
    tabs = [driver.Tab(job=b, adapter=adapter_for(b.ats, parse_wait_s=5)) for b in batch if b]
    for t in tabs:
        driver.fill_tab(browser_context, t, paths.screens)
        assert t.status == "ready_for_review", t.review
        assert t.guard is not None and not t.guard.active  # released at handoff
        driver.mark_prefilled(paths, t.job.job_id)
        assert status_of(home, t.job.job_id)[0] == "Prefilled"
    assert not [r for r in router.requests if r["method"] != "GET" and
                any(s in r["url"] for s in SUBMITS)]  # fmt: skip

    gh = next(t for t in tabs if t.job.ats == "greenhouse")
    lv = next(t for t in tabs if t.job.ats != "greenhouse")
    gh.page.locator("button[type=submit]").click()  # the human submits this one
    lv.page.close()  # and closes another without submitting
    applied = []

    def on_applied(t):
        applied.append(t.job.job_id)
        driver.mark_applied(paths, t.job.job_id, date(2026, 10, 18))

    driver.watch(tabs, on_applied, lambda: None, deadline=time.monotonic() + 8,
                 should_stop=lambda: False, poll_s=0.2)  # fmt: skip
    assert applied == [gh.job.job_id]
    assert status_of(home, gh.job.job_id) == ("Applied", "2026-10-18")
    assert lv.status == "closed" and status_of(home, lv.job.job_id)[0] == "Prefilled"


def test_detached_driver_end_to_end(cli, persona_home, fx_all):
    """The real background process, headless on fixtures: fills, hands off, stops cleanly."""
    pytest.importorskip("playwright")
    home = setup_persona(cli, persona_home, "senior-backend", fx_all)
    code, env, _ = cli("prefill", "--fixtures", str(fx_all), "--headless")
    if code != ExitCode.OK and "didn't start" in json.dumps(env):
        pytest.skip("no browser binary")
    assert code == ExitCode.OK, env
    assert env["data"]["running"] and env["hints"]["user_actions"]
    queued = [j["job_id"] for j in env["data"]["jobs"] if j["status"] == "queued"]
    assert queued

    code, again, _ = cli("prefill", "--fixtures", str(fx_all), "--headless")
    assert code == ExitCode.LOCKED, again  # one batch at a time

    deadline = time.monotonic() + 90
    st = {}
    while time.monotonic() < deadline:
        code, st, _ = cli("prefill", "--status")
        if st["data"]["driver"] in ("waiting_for_you", "done", "stopped"):
            break
        time.sleep(0.5)
    d = st["data"]
    assert d["driver"] == "waiting_for_you", (d, (home / "logs" / "prefill.log").read_text())
    assert {j["status"] for j in d["jobs"]} == {"ready_for_review"}
    for j in d["jobs"]:
        assert j["review"]["filled"] == j["review"]["to_fill"]
        assert Path(j["review"]["screenshot"]).exists()
        assert status_of(home, j["job_id"])[0] == "Prefilled"
    for s in secrets_of("senior-backend"):
        assert s not in json.dumps(st)
    log = (home / "work" / "prefill" / "fixture-requests.jsonl").read_text().splitlines()
    sent = [json.loads(x) for x in log]
    assert not [r for r in sent if r["method"] != "GET" and any(s in r["url"] for s in SUBMITS)]

    code, st, _ = cli("prefill", "--stop")
    assert code == ExitCode.OK
    assert st["data"]["driver"] == "stopped" and not st["data"]["running"]
    for j in st["data"]["jobs"]:
        assert j["status"] == "stopped"
        assert status_of(home, j["job_id"])[0] == "Prefilled"  # closed without submitting
