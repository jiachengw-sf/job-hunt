"""`jobhunt prefill` end to end on fixtures: batch, Prefilled, handoff, Applied, never submit."""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
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
    prof = yaml.safe_load((home / "profile.yaml").read_text(encoding="utf-8"))
    prof["resumes"] = [{"file": "resume.pdf", "default": True}]
    (home / "profile.yaml").write_text(yaml.safe_dump(prof, sort_keys=False), encoding="utf-8")
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
        driver.record_confirmed(paths, t, now="2026-10-18T12:00:00Z")

    driver.watch(tabs, on_applied, lambda: None, deadline=time.monotonic() + 8,
                 should_stop=lambda: False, poll_s=0.2)  # fmt: skip
    assert applied == [gh.job.job_id]
    assert status_of(home, gh.job.job_id) == ("Applied", "2026-10-18")
    assert lv.status == "closed" and status_of(home, lv.job.job_id)[0] == "Prefilled"
    assert_prefill_record(home, gh, lv)
    driver.record_confirmed(paths, gh, now="2026-10-18T12:05:00Z")  # seen again: still one
    with db(home) as c:
        assert c.execute("SELECT count(*) FROM application_records").fetchone()[0] == 1


def assert_prefill_record(home: Path, gh, lv) -> None:
    """The confirmation created exactly one record, with its proof and where to check."""
    with db(home) as c:
        recs = c.execute("SELECT * FROM application_records").fetchall()
        assert [r["job_id"] for r in recs] == [gh.job.job_id]  # none for the closed tab
        r = recs[0]
        events = c.execute(
            "SELECT source_id, details_json FROM events WHERE job_id=? AND kind='status_changed'",
            (gh.job.job_id,),
        ).fetchall()
    assert r["method"] == "prefilled" and r["recorded_via"] == "prefill"
    assert r["applied_at"] == "2026-10-18T12:00:00Z" and not r["applied_at_approx"]
    assert r["confirmation_id"]  # Greenhouse's application_id from the confirmation URL
    assert r["status_check_method"] == "portal" and r["status_check_url"].startswith(
        "https://my.greenhouse.io"
    )
    assert r["resume_file"] == "resume.pdf" and len(r["resume_sha256"]) == 64
    sent = json.loads(r["submitted_json"])
    assert sent and all(set(s) == {"label", "kind", "sensitive", "key"} for s in sent)
    folder = home / "applications" / r["folder"]
    assert r["folder"].startswith("2026-10-1")
    for f in ("README.md", "job_description.md", "submitted_answers.md", "confirmation.png",
              "resume.pdf"):  # fmt: skip
        assert (folder / f).exists(), f
    assert "captured" in (folder / "job_description.md").read_text(encoding="utf-8")
    assert "MyGreenhouse" in (folder / "README.md").read_text(encoding="utf-8")
    assert json.loads(events[-1]["details_json"])["to"] == "Applied"
    assert events[-1]["source_id"] == "agent"


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
    assert d["driver"] == "waiting_for_you", (
        d,
        (home / "logs" / "prefill.log").read_text(encoding="utf-8"),
    )
    assert {j["status"] for j in d["jobs"]} == {"ready_for_review"}
    for j in d["jobs"]:
        assert j["review"]["filled"] == j["review"]["to_fill"]
        assert Path(j["review"]["screenshot"]).exists()
        assert status_of(home, j["job_id"])[0] == "Prefilled"
    for s in secrets_of("senior-backend"):
        assert s not in json.dumps(st)
    log = (
        (home / "work" / "prefill" / "fixture-requests.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    sent = [json.loads(x) for x in log]
    assert not [r for r in sent if r["method"] != "GET" and any(s in r["url"] for s in SUBMITS)]

    code, st, _ = cli("prefill", "--stop")
    assert code == ExitCode.OK
    assert st["data"]["driver"] == "stopped" and not st["data"]["running"]
    for j in st["data"]["jobs"]:
        assert j["status"] == "stopped"
        assert status_of(home, j["job_id"])[0] == "Prefilled"  # closed without submitting


def two_resumes_no_default(home: Path, persona: str) -> None:
    """The persona keeps a data resume and a software resume, and no default."""
    shutil.copy(RESUMES / "newgrad-analyst.pdf", home / "resume" / "data.pdf")
    shutil.copy(RESUMES / f"{persona}.pdf", home / "resume" / "software.pdf")
    onboarding = home / "work" / "onboarding"
    onboarding.mkdir(parents=True, exist_ok=True)
    for name, src in (("data", "newgrad-analyst"), ("software", persona)):
        text = (REPO / "tests" / "personas" / src / "resume.md").read_text(encoding="utf-8")
        (onboarding / f"{name}.redacted.txt").write_text(text, encoding="utf-8")
    prof = yaml.safe_load((home / "profile.yaml").read_text(encoding="utf-8"))
    prof["resumes"] = [{"file": "data.pdf"}, {"file": "software.pdf"}]
    (home / "profile.yaml").write_text(yaml.safe_dump(prof, sort_keys=False), encoding="utf-8")


def test_several_resumes_and_no_default_ask_per_job(cli, persona_home, fx_all):
    home = setup_persona(cli, persona_home, "intl-sponsorship", fx_all)
    two_resumes_no_default(home, "intl-sponsorship")
    assert get_paths().onboarding == home / "work" / "onboarding"
    code, env, _ = cli("prefill", "--dry-run", "--batch", "20")
    assert code == ExitCode.OK, env
    jobs = env["data"]["jobs"]
    asked = [j for j in jobs if (j["reason"] or "").startswith("several resumes")]
    assert asked and not [j for j in jobs if j["status"] == "queued"]
    assert all(j["suggested_resume"] == "software.pdf" for j in asked)  # a software posting
    assert any("--resume" in a for a in env["hints"]["next_actions"])
    code, env, _ = cli("prefill", "--dry-run", "--batch", "20", "--resume", "data.pdf")
    queued = [j for j in env["data"]["jobs"] if j["status"] == "queued"]
    assert queued and {j["resume"] for j in queued} == {"data.pdf"}
    code, env, _ = cli("prefill", "--dry-run", "--resume", "missing.pdf")
    assert code == ExitCode.VALIDATION


def test_the_resume_chosen_for_a_job_is_kept(cli, persona_home, fx_all):
    """answers.json keeps the choice: a later prefill and `applied` use it."""
    from jobhunt.cli.prefill import choose_resume
    from jobhunt.cli.track import _resume
    from jobhunt.prefill.values import AnswerSheet, write_sheet

    home = setup_persona(cli, persona_home, "intl-sponsorship", fx_all)
    two_resumes_no_default(home, "intl-sponsorship")
    paths = get_paths()
    assert choose_resume(paths, 7, None) is None and _resume(paths, None, 7) is None
    sheet = AnswerSheet(job_id=7, ats="greenhouse", url=None, built_at="2026-10-23T00:00:00Z",
                        resume=str(home / "resume" / "software.pdf"))  # fmt: skip
    write_sheet(paths.prefill / "7" / "answers.json", sheet)
    assert choose_resume(paths, 7, None) == home / "resume" / "software.pdf"
    assert _resume(paths, None, 7) == home / "resume" / "software.pdf"
    assert choose_resume(paths, 7, home / "resume" / "data.pdf").name == "data.pdf"
