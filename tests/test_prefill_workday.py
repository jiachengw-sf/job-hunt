"""Workday pre-fill against a synthetic Workday-style site (tests/fixtures/forms/pages/
northwind.wd1.myworkdayjobs.com): the human signs in, jobhunt fills each step from the profile and
answer bank, stops for what only the human can answer, and never submits."""

from __future__ import annotations

import shutil

import pytest

from jobhunt.cli.prefill import workday_apply_url
from jobhunt.prefill.adapters import adapter_for
from jobhunt.prefill.adapters.workday import parse_ym
from jobhunt.prefill.fixtures import FixtureRouter
from jobhunt.prefill.guard import SubmitRefused, search_enter, search_refusal
from jobhunt.prefill.workday_flow import Inputs, WorkdayRun
from tests.conftest import PERSONAS, REPO
from tests.test_apply_cli import BASE, OVERLAY, db
from tests.test_forms import ctx_for

URL = "https://northwind.wd1.myworkdayjobs.com/en-US/careers/job/Remote/Software-Intern_R-1001/apply/applyManually"
RESUMES = REPO / "tests" / "fixtures" / "resumes"


def make_run(persona: str, tmp_path) -> WorkdayRun:
    ctx, matcher, eeo = ctx_for(persona, "Northwind")
    ctx.resume = RESUMES / f"{persona}.pdf"
    return WorkdayRun(
        job_id=1, company="Northwind", title="Software Intern", url=URL, sheet_dir=tmp_path,
        screens=tmp_path / "screens", inputs=Inputs(ctx, matcher, eeo), now="2026-11-06T12:00:00Z",
    )  # fmt: skip


def open_page(browser_context, pages):
    router = FixtureRouter(pages)
    router.install(browser_context)
    page = browser_context.new_page()
    page.goto(URL)
    page.wait_for_selector("[data-automation-id=progressBarActiveStep]")
    return page, router


def human(page, selector: str) -> None:
    """The human's own click, outside the engine (it may land under the sticky footer)."""
    page.locator(selector).first.evaluate("e => e.click()")
    page.wait_for_timeout(300)


def tick_until(run: WorkdayRun, page, statuses: set[str], rounds: int = 12) -> str:
    for _ in range(rounds):
        run.tick(page)
        if run.status in statuses:
            return run.status
        page.wait_for_timeout(500)
    return run.status


def submits(router: FixtureRouter) -> list[dict]:
    return [r for r in router.requests if r["method"] != "GET" and "submit" in r["url"]]


def state(page) -> dict:
    return page.evaluate("() => window.__wd.v")


def test_waits_for_the_human_to_sign_in(browser_context, fx_pages, tmp_path):
    page, router = open_page(browser_context, fx_pages)
    run = make_run("first-year-intern", tmp_path)
    assert run.tick(page) is False or run.status == "waiting_sign_in"
    assert run.status == "waiting_sign_in" and not run.steps
    # jobhunt typed and clicked nothing; the sign-in request is the human's, and goes through
    human(page, "#human-sign-in")
    assert any("/careers/login" in r["url"] for r in router.requests)
    page.wait_for_selector("[data-automation-id=formField-source]")
    assert tick_until(run, page, {"filling", "needs_you", "ready_for_review"}) != "waiting_sign_in"


def test_fills_each_step_stops_for_attestations_and_never_submits(
    browser_context, fx_pages, tmp_path
):
    page, router = open_page(browser_context, fx_pages)
    run = make_run("first-year-intern", tmp_path)
    human(page, "#human-sign-in")
    page.wait_for_selector("[data-automation-id=formField-source]")

    # My Information and My Experience fill and save themselves; the questions step has a
    # required "I certify" checkbox, which is the human's
    assert tick_until(run, page, {"needs_you", "ready_for_review"}, rounds=30) == "needs_you"
    names = [s.name for s in run.steps]
    assert names[:3] == ["My Information", "My Experience", "Application Questions"], names
    assert run.steps[0].advanced and run.steps[1].advanced and not run.steps[2].advanced
    v = state(page)
    assert v["name--legalName--firstName"] == "Riley"
    assert v["name--legalName--lastName"] == "Fixture"
    assert v["source--source"] == ["Company Website"]
    assert v["candidateIsPreviousWorker"] == "No"  # not in the profile's employers
    assert v["phoneNumber--phoneNumber"].replace("-", "").endswith("3125550101")
    # My Experience from the profile: the job, its From date, the school by Enter search, the
    # field of study from the long "All" list, the resume, the GitHub link
    assert v["workExperience-11--jobTitle"] == "Student Technology Assistant"
    assert v["workExperience-11--currentlyWorkHere"] is True
    start = (v["workExperience-11--startDate-m"], v["workExperience-11--startDate-y"])
    assert start == ("09", "2026")
    assert v["education-21--school"] == ["Northfield State University"]
    assert v["education-21--degree"] == "Bachelors"
    assert v["education-21--fieldOfStudy"] == ["Computer Science"]
    assert page.evaluate("() => window.__wd.resume") == "first-year-intern.pdf"
    assert v["webAddress-41--url"] == "https://github.com/riley-fixture-example"
    # the questions from the bank; the attestation left for the human
    assert v["primaryQuestionnaire--q0a1"] == "Yes"
    assert v["primaryQuestionnaire--q0a2"] == "No"
    assert not v.get("primaryQuestionnaire--q0a4")
    assert any("certify" in i.label.lower() for i in run.steps[2].needs_you)
    assert submits(router) == []

    # the human ticks it and saves; jobhunt picks up Voluntary Disclosures, whose consent is
    # the human's too
    human(page, "#primaryQuestionnaire--q0a4")
    human(page, "[data-automation-id=pageFooterNextButton]")
    assert tick_until(run, page, {"needs_you", "ready_for_review"}) == "needs_you"
    assert run.steps[-1].name == "Voluntary Disclosures"
    human(page, "#termsAndConditions--acceptTermsAndAgreements")
    human(page, "[data-automation-id=pageFooterNextButton]")
    assert tick_until(run, page, {"ready_for_review"}) == "ready_for_review"
    assert run.review().status == "ready_for_review"
    assert submits(router) == []  # nothing jobhunt did reached a submit endpoint

    # the human submits; the confirmation is seen
    ad = run.adapter
    assert not ad.confirmed(page)
    human(page, "[data-automation-id=pageFooterNextButton]")
    page.wait_for_selector("text=Application Submitted")
    assert ad.confirmed(page)
    assert len(submits(router)) == 1


def test_a_value_the_page_lacks_is_left_for_the_human(browser_context, fx_pages, tmp_path):
    """A source answer with no option on this form stops the step instead of guessing."""
    page, _ = open_page(browser_context, fx_pages)
    run = make_run("first-year-intern", tmp_path)
    run.inputs.ctx.answers.sourcing.how_did_you_hear = "Billboard"
    human(page, "#human-sign-in")
    page.wait_for_selector("[data-automation-id=formField-source]")
    assert tick_until(run, page, {"needs_you", "ready_for_review"}) == "needs_you"
    assert run.steps[0].name == "My Information" and not run.steps[0].advanced
    assert not state(page).get("source--source")


@pytest.mark.parametrize(
    ("method", "path", "ok"),
    [
        ("POST", "/wday/calypso/cxs/jobapplication/acme/jobapplication/ab12/phonenumber", True),
        ("PUT", "/wday/calypso/cxs/jobapplication/acme/package/ab12/validate", True),
        ("POST", "/wday/calypso/cxs/common/acme/questionnaire/q1/definition?type=primary", True),
        ("PUT", "/wday/cxs/acme/jobapplication/ab12/termsandconditions", True),
        ("POST", "/wday/cxs/acme/jobapplication/ab12/submit", False),
        ("POST", "/wday/cxs/acme/jobapplication/ab12/submitApplication", False),
        ("POST", "/wday/calypso/cxs/jobapplication/acme/jobapplication/ab12/submit", False),
        ("POST", "/wday/cxs/acme/careers/login", False),  # default deny while filling
    ],
)
def test_workday_guard(method, path, ok):
    g = adapter_for("workday").guard()
    assert g.permits(method, "https://acme.wd5.myworkdayjobs.com" + path) is ok


def test_enter_only_in_a_workday_search_box():
    assert search_refusal({"aid": "searchBox", "tag": "input", "type": "text"}) is None
    assert search_refusal({"aid": "searchBox", "tag": "input", "type": "text", "in_form": True})
    assert search_refusal({"aid": "", "tag": "input", "type": "text"})
    assert search_refusal({"aid": "searchBox", "tag": "button", "type": "submit"})

    class Fake:
        def __init__(self, el):
            self.el, self.pressed = el, []

        def evaluate(self, _js):
            return self.el

        def press(self, key):
            self.pressed.append(key)

    ok = Fake({"aid": "searchBox", "tag": "input", "type": "text", "in_form": False})
    search_enter(ok)
    assert ok.pressed == ["Enter"]
    bad = Fake({"aid": "email", "tag": "input", "type": "text", "in_form": True})
    with pytest.raises(SubmitRefused):
        search_enter(bad)
    assert bad.pressed == []


@pytest.mark.parametrize(
    ("v", "want"),
    [
        ("2030-05", (5, None, 2030)),
        ("2027-05-24", (5, 24, 2027)),
        ("05/2030", (5, None, 2030)),
        ("May 2030", (5, None, 2030)),
        ("2030", (None, None, 2030)),
        ("soon", (None, None, None)),
    ],
)
def test_parse_ym(v, want):
    assert parse_ym(v) == want


def test_workday_apply_url():
    base = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Remote/Intern_R-1"
    assert workday_apply_url(base + "?utm=x") == base + "/apply/applyManually"
    assert workday_apply_url(base + "/apply") == base + "/apply/applyManually"
    assert workday_apply_url("https://acme.wd5.myworkdayjobs.com/en-US/External") is None
    assert workday_apply_url(None) is None


def test_workday_has_no_fixed_answers_in_the_code():
    """Values come from the profile and bank only: the adapter carries no canned answers."""
    path = REPO / "src" / "jobhunt" / "prefill" / "adapters" / "workday.py"
    src = path.read_text(encoding="utf-8")
    for canned in ("Riley", "Spanish", "Aalborg", "123 Fixture", "Under $20,000"):
        assert canned not in src


@pytest.mark.parametrize("persona", PERSONAS)
def test_first_step_never_guesses_for_any_persona(browser_context, fx_pages, tmp_path, persona):
    wrong = {"mismatch", "error", "refused", "not_found"}  # left-for-you outcomes are fine

    page, router = open_page(browser_context, fx_pages)
    run = make_run(persona, tmp_path)
    human(page, "#human-sign-in")
    page.wait_for_selector("[data-automation-id=formField-source]")
    tick_until(run, page, {"needs_you", "ready_for_review"}, rounds=30)
    first = run.steps[0]
    assert first.name == "My Information"
    assert not [i for i in first.needs_you if i.status in wrong], first.needs_you
    v = state(page)
    ctx = run.inputs.ctx
    assert v["name--legalName--firstName"] == ctx.answers.identity.legal_first_name
    if not first.advanced:  # stopped: only for a required field the bank can't answer
        assert first.empty or first.needs_you
    assert submits(router) == []


@pytest.fixture(scope="module")
def sources_fx(tmp_path_factory):
    d = tmp_path_factory.mktemp("sources")
    shutil.copytree(BASE, d, dirs_exist_ok=True)
    shutil.copytree(OVERLAY, d, dirs_exist_ok=True)
    return d


def test_prefill_queues_a_workday_job_for_the_humans_sign_in(cli, persona_home, sources_fx):
    home = persona_home("first-year-intern")
    cli("run", "--fixtures", str(sources_fx), "--no-sync", "--no-inspect")
    with db(home) as c:
        jid = c.execute("SELECT id FROM jobs WHERE ats='workday' LIMIT 1").fetchone()[0]
    code, env, _ = cli("prefill", str(jid), "--dry-run")
    assert code == 0, env
    j = env["data"]["jobs"][0]
    assert j["status"] == "queued" and j["ats"] == "workday"
    assert "sign in" in j["reason"]
