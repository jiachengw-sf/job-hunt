"""Adapters against saved fixture forms: fill, read back, never submit, detect confirmation."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobhunt.forms.classify import classify
from jobhunt.prefill.adapters import adapter_for
from jobhunt.prefill.adapters.base import phone_country_option, pick_location
from jobhunt.prefill.fixtures import FixtureRouter
from jobhunt.prefill.values import build_sheet, validate
from tests.conftest import PERSONAS, REPO
from tests.test_forms import COMPANY, SPECS, ctx_for

RESUMES = REPO / "tests" / "fixtures" / "resumes"
URLS = {
    "gh_A": "https://job-boards.greenhouse.io/northwind/jobs/1001",
    "lv_A": "https://jobs.lever.co/bluefin/4d1c2e7a-0b6f-4c1e-9a7d-2f3b5c6d7e01/apply",
    "ab_A": "https://jobs.ashbyhq.com/quillstack/9e8d7c6b-5a49-4382-9170-6f5e4d3c2b01/application",
}
ATS = {"gh": "greenhouse", "lv": "lever", "ab": "ashby"}
SUBMITS = ("/jobs/1001", "/apply", "op=ApiSubmit")


@pytest.fixture
def fx(fx_pages) -> Path:
    return fx_pages


def make_sheet(persona: str, name: str, resume: Path):
    spec = SPECS[name]()
    spec.url = URLS[name]
    ctx, matcher, eeo = ctx_for(persona, COMPANY[name[:2]])
    ctx.resume = resume
    cls = classify(spec, ctx, matcher, eeo_confirmed=eeo)
    assert cls.cls == "ready", cls.reasons
    sheet = build_sheet(1, spec, cls, ctx, matcher, "2026-10-18T12:00:00Z")
    assert validate(sheet, spec) == []
    return sheet


def submit_posts(router: FixtureRouter) -> list[dict]:
    return [
        r for r in router.requests
        if r["method"] != "GET" and any(s in r["url"] for s in SUBMITS)
    ]  # fmt: skip


def run_fill(browser_context, fx, name, sheet):
    router = FixtureRouter(fx)
    router.install(browser_context)
    page = browser_context.new_page()
    adapter = adapter_for(ATS[name[:2]], parse_wait_s=5)
    guard = adapter.guard()
    guard.install(page)
    page.goto(URLS[name])
    outcomes = adapter.fill(page, sheet)
    return page, adapter, guard, router, outcomes


def human_submit(page, name):
    sel = {
        "gh": "button[type=submit]",
        "lv": "#btn-submit",
        "ab": ".ashby-application-form-submit-button",
    }[name[:2]]
    page.locator(sel).click()  # the human's click, outside the engine
    page.wait_for_timeout(800)


@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize("name", sorted(URLS))
def test_fill_reads_back_and_stops_before_submit(browser_context, fx, persona, name):
    sheet = make_sheet(persona, name, RESUMES / f"{persona}.pdf")
    page, adapter, guard, router, outcomes = run_fill(browser_context, fx, name, sheet)
    by = {o.id: o for o in outcomes}
    for ff in sheet.to_fill():
        assert by[ff.id].status == "filled", (ff.id, by[ff.id].status, by[ff.id].note)
    assert not submit_posts(router), "a submit request left the page during the fill"
    assert not adapter.confirmed(page)
    for o in outcomes:  # values never serialize
        assert "expected" not in o.model_dump() and "actual" not in o.model_dump()

    guard.release(page)
    human_submit(page, name)
    assert submit_posts(router), "after handoff the human's submit must go through"
    assert adapter.confirmed(page)


def test_greenhouse_specifics(browser_context, fx):
    sheet = make_sheet("intl-sponsorship", "gh_A", RESUMES / "intl-sponsorship.pdf")
    page, _, _, router, outcomes = run_fill(browser_context, fx, "gh_A", sheet)
    by = {o.id: o for o in outcomes}
    assert by["phone_country"].status == "filled"  # set before the number
    assert page.locator(".iti__selected-country").get_attribute("title") == "United States"
    assert by["location"].actual == "Lakeview, Massachusetts, United States"  # clicked option
    assert page.locator(".select__single-value").first.inner_text()
    assert any("amazonaws.com" in r["url"] and r["method"] == "POST" for r in router.requests)
    eeo = [o for o in outcomes if o.id in ("gender", "race", "veteran_status")]
    assert all(o.status in ("left", "skipped", "filled") for o in eeo)


def eeo_sheet(hispanic: str):
    """gh_A with EEO confirmed and these synthetic Hispanic/Latino and race answers."""
    spec = SPECS["gh_A"]()
    spec.url = URLS["gh_A"]
    ctx, matcher, _ = ctx_for("intl-sponsorship", COMPANY["gh"])
    ctx.resume = RESUMES / "intl-sponsorship.pdf"
    ctx.answers.eeo.hispanic_latino = hispanic
    ctx.answers.eeo.race_ethnicity = "Asian"
    cls = classify(spec, ctx, matcher, eeo_confirmed=True)
    sheet = build_sheet(1, spec, cls, ctx, matcher, "2026-10-18T12:00:00Z")
    assert validate(sheet, spec) == []
    return sheet


def test_greenhouse_race_appears_after_hispanic_no(browser_context, fx):
    _, _, _, router, outcomes = run_fill(browser_context, fx, "gh_A", eeo_sheet("No"))
    by = {o.id: o for o in outcomes}
    assert by["hispanic_ethnicity"].status == "filled"
    assert by["race"].status == "filled" and by["race"].actual == "Asian"
    assert not submit_posts(router)


def test_greenhouse_race_stays_hidden_after_hispanic_yes(browser_context, fx):
    page, _, _, _, outcomes = run_fill(browser_context, fx, "gh_A", eeo_sheet("Yes"))
    by = {o.id: o for o in outcomes}
    assert by["hispanic_ethnicity"].status == "filled"
    assert by["race"].status == "skipped"  # the page sets it; never looked for
    assert page.locator("#race").count() == 0


def test_greenhouse_rejects_unaccepted_resume_type(browser_context, fx, tmp_path):
    md = tmp_path / "resume.md"
    md.write_text("# Synthetic resume\n", encoding="utf-8")
    sheet = make_sheet("intl-sponsorship", "gh_A", md)
    _, _, _, router, outcomes = run_fill(browser_context, fx, "gh_A", sheet)
    by = {o.id: o for o in outcomes}
    assert by["resume"].status == "file_rejected"
    assert ".pdf" in (by["resume"].note or "")
    assert not any("amazonaws" in r["url"] for r in router.requests)


def test_lever_radios_are_recommended_not_ticked(browser_context, fx):
    sheet = make_sheet("intl-sponsorship", "lv_A", RESUMES / "intl-sponsorship.pdf")
    page, *_, outcomes = run_fill(browser_context, fx, "lv_A", sheet)
    recs = [o for o in outcomes if o.status == "recommend"]
    assert len(recs) == 2 and all(o.expected for o in recs)
    assert page.locator("input[type=radio]:checked").count() == 0


@pytest.mark.parametrize("name", ["lv_A", "ab_A"])
def test_resume_parse_does_not_overwrite_our_values(browser_context, fx, name):
    """The ATS autofills from the resume; the engine uploads first, then overwrites."""
    sheet = make_sheet("intl-sponsorship", name, RESUMES / "intl-sponsorship.pdf")
    if name == "lv_A":  # as if the user had no current company to give
        org = next(f for f in sheet.fields if f.id == "org")
        org.action, org.value = "skip", None
    _, _, _, router, outcomes = run_fill(browser_context, fx, name, sheet)
    assert any("parseResume" in r["url"] or "Autofill" in r["url"] for r in router.requests)
    email = next(o for o in outcomes if o.label == "Email")
    assert email.status == "filled" and "parsed" not in (email.actual or "")
    if name == "lv_A":  # an optional field the parse filled is flagged, never silently kept
        org = next(o for o in outcomes if o.id == "org")
        assert org.status == "autofilled"


def test_ashby_parse_wins_if_filled_first(browser_context, fx):
    """Control: filling before the upload lets the parse overwrite, and read-back catches it."""
    sheet = make_sheet("intl-sponsorship", "ab_A", RESUMES / "intl-sponsorship.pdf")
    adapter_cls = type(adapter_for("ashby"))

    class FillFirst(adapter_cls):  # type: ignore[valid-type, misc]
        upload_first = False

        def order(self, s):
            return sorted(s.to_fill(), key=lambda f: f.type == "file")

    router = FixtureRouter(fx)
    router.install(browser_context)
    page = browser_context.new_page()
    a = FillFirst(parse_wait_s=5)
    a.guard().install(page)
    page.goto(URLS["ab_A"])
    outcomes = a.fill(page, sheet)
    assert next(o for o in outcomes if o.label == "Email").status == "mismatch"


def test_missing_field_is_reported_not_guessed(browser_context, fx):
    sheet = make_sheet("intl-sponsorship", "gh_A", RESUMES / "intl-sponsorship.pdf")
    ff = next(f for f in sheet.fields if f.id == "question_100101")
    ff.action, ff.value, ff.id = "fill", "https://example.com/x", "question_999999"
    *_, outcomes = run_fill(browser_context, fx, "gh_A", sheet)
    o = next(o for o in outcomes if o.id == "question_999999")
    assert o.status == "not_found"


def test_pick_location_needs_city_and_region():
    opts = ["Lakeview, Massachusetts, United States", "Lakeview, Oregon, United States"]
    assert pick_location("Lakeview, MA", opts) == opts[0]
    assert pick_location("Lakeview, Oregon", opts) == opts[1]
    assert pick_location("Lakeview", opts) is None  # ambiguous: never guess
    assert pick_location("Lakeview, TX", opts) is None
    assert pick_location("Lakeview, MA", ["Lakeview, MA, USA"]) == "Lakeview, MA, USA"


def test_phone_country_option():
    opts = ["United States +1", "Canada +1", "India +91"]
    assert phone_country_option("CA", opts) == "Canada +1"
    assert phone_country_option("US", opts) == "United States +1"
    assert phone_country_option("FR", opts) is None
    assert phone_country_option(None, opts) is None
