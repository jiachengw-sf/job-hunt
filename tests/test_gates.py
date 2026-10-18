from __future__ import annotations

import pytest

from jobhunt.fit import gates as gt
from jobhunt.models.answers import WorkAuthorization
from tests.fit_helpers import gate, job, persona_ctx

INTERN = "first-year-intern"
INTL = "intl-sponsorship"
NEWGRAD = "newgrad-analyst"
SENIOR = "senior-backend"


def run(persona: str, j, ctx=None):
    return gt.run_gates(j, ctx or persona_ctx(persona))


# --- preference gates -----------------------------------------------------------------------


def test_seniority_fail_and_unverified():
    r = run(NEWGRAD, job("Senior Data Analyst", employment_type="Full-time"))
    assert gate(r, "seniority").result == "fail"
    r = run(SENIOR, job("Software Engineer", work_mode="remote", locations=["Remote - US"]))
    assert gate(r, "seniority").result == "pass_unverified"


def test_new_grad_and_entry_are_compatible():
    r = run(NEWGRAD, job("Data Analyst - Entry Level", employment_type="Full-time"))
    assert gate(r, "seniority").result == "pass"


def test_employment_type():
    r = run(INTERN, job("Data Analyst", employment_type="Full-time"))
    assert gate(r, "employment_type").result == "fail"
    assert gate(run(INTERN, job()), "employment_type").result == "pass"


def test_term_and_timing():
    r = run(INTERN, job("Software Intern, Fall 2027"))
    assert gate(r, "term").result == "fail"
    r = run(NEWGRAD, job("Data Analyst, Spring 2027 start", employment_type="Full-time"))
    assert gate(r, "timing").result == "fail"  # must start on/after 2027-06-01
    assert gate(run(INTERN, job("Software Intern")), "term").result == "pass_unverified"


@pytest.mark.parametrize(
    ("locs", "expected"),
    [
        (["Toronto, ON"], "fail"),
        (["Austin, TX"], "pass"),
        (["Remote - US"], "pass"),
        (["Remote"], "pass_unverified"),
        (["London, UK", "New York, NY"], "pass"),
    ],
)
def test_country(locs, expected):
    assert gate(run(INTERN, job(locations=locs)), "country").result == expected


def test_work_mode_remote_must():
    r = run(
        SENIOR, job("Senior Backend Engineer", locations=["San Francisco, CA"], work_mode="onsite")
    )
    assert gate(r, "work_mode").result == "fail"
    r = run(SENIOR, job("Senior Backend Engineer", locations=["Remote - US"]))
    assert gate(r, "work_mode").result == "pass"


def test_places_radius(tmp_path):
    ctx = persona_ctx(NEWGRAD)
    ctx.prefs.location.places.importance = "must"
    near = job("Data Analyst", locations=["Cambridge, MA"], employment_type="Full-time")
    far = job("Data Analyst", locations=["Austin, TX"], employment_type="Full-time")
    unknown = job("Data Analyst", locations=["Smallville, KS"], employment_type="Full-time")
    assert gate(gt.run_gates(near, ctx), "places").result == "pass"
    assert gate(gt.run_gates(far, ctx), "places").result == "fail"
    assert gate(gt.run_gates(unknown, ctx), "places").result == "pass_unverified"


def test_pay_must_normalizes_hourly_and_flags_missing():
    low = {"min": 25, "max": 30, "interval": "per-hour-wage"}  # 62,400/yr < 65,000
    ok = {"min": 70000, "max": 80000, "interval": "per-year-salary"}
    mk = lambda p: job("Data Analyst", employment_type="Full-time", pay=p)  # noqa: E731
    assert gate(run(NEWGRAD, mk(low)), "pay").result == "fail"
    assert gate(run(NEWGRAD, mk(ok)), "pay").result == "pass"
    missing = gate(run(NEWGRAD, mk(None)), "pay")
    assert missing.result == "pass_unverified" and "never excluded" in missing.reason


def test_blocklist_and_title_keywords():
    r = run(SENIOR, job("Senior Backend Engineer", company="Ledgerly", locations=["Remote - US"]))
    assert gate(r, "blocklist").result == "fail"
    r = run(SENIOR, job("Senior Frontend Engineer", locations=["Remote - US"]))
    assert gate(r, "exclude_keywords").result == "fail"
    r = run(INTERN, job("PhD Software Intern"))
    assert gate(r, "exclude_keywords").result == "fail"


def test_avoid_industries_company_fails_description_is_unclear():
    r = run(SENIOR, job("Senior Engineer", company="Moonpath Crypto", locations=["Remote - US"]))
    assert gate(r, "avoid_industries").result == "fail"
    r = run(SENIOR, job("Senior Engineer", "We build a crypto wallet.", locations=["Remote - US"]))
    g = gate(r, "avoid_industries")
    assert g.result == "unclear" and g.route == "ai"


# --- knockouts ------------------------------------------------------------------------------

NO_SPONSOR = (
    "Requirements\n"
    "- Must be authorized to work in the US without current or future visa sponsorship"
)


def test_sponsorship_fails_only_for_who_needs_it():
    j = job(description=NO_SPONSOR)
    assert gate(run(INTL, j), "sponsorship").result == "fail"
    assert gate(run(INTERN, j), "sponsorship").result == "pass"


def test_sponsorship_unanswered_goes_to_the_user():
    ctx = persona_ctx(INTERN)
    ctx.answers.work_authorization.clear()
    g = gate(gt.run_gates(job(description=NO_SPONSOR), ctx), "sponsorship")
    assert g.result == "unclear" and g.route == "user"


def test_sponsorship_vague_is_unclear():
    j = job(description="Visa sponsorship may be available for exceptional candidates.")
    g = gate(run(INTL, j), "sponsorship")
    assert g.result == "unclear" and g.route == "ai"
    assert gate(run(INTERN, j), "sponsorship").result == "pass"


def test_canada_sponsorship_uses_canada_answers():
    j = job(description="We cannot sponsor work permits.", locations=["Toronto, ON"])
    assert gate(run(INTL, j), "sponsorship").result == "fail"


def test_citizenship():
    j = job(description="Requirements\n- U.S. citizenship required")
    assert gate(run(INTL, j), "citizenship").result == "fail"
    g = gate(run(INTERN, j), "citizenship")
    assert g.result == "unclear" and g.route == "user"  # citizenship not answered
    ctx = persona_ctx(INTERN)
    ctx.answers.work_authorization["US"] = WorkAuthorization(authorized="yes", citizen="yes")
    assert gate(gt.run_gates(j, ctx), "citizenship").result == "pass"


def test_clearance():
    j = job(description="Requirements\n- Active Secret clearance required")
    assert gate(run(INTL, j), "clearance").result == "fail"  # answered "none"
    g = gate(run(NEWGRAD, j), "clearance")  # not answered
    assert g.result == "unclear" and g.route == "user"


@pytest.mark.parametrize(
    ("text", "persona", "expected"),
    [
        ("Graduating between Dec 2027 and Jun 2028", INTL, "pass"),
        ("Graduating between Dec 2027 and Jun 2028", INTERN, "fail"),
        ("For students graduating in 2028.", INTL, "fail"),
        ("Class of 2030", INTERN, "pass"),
        ("Open to freshman and sophomore students.", INTERN, "pass"),
        ("Open to rising juniors and seniors (undergraduate students).", INTERN, "fail"),
        (
            "Open to rising sophomores, juniors and seniors (undergraduate students).",
            INTERN,
            "pass",
        ),
        ("Open to rising sophomores, juniors and seniors (undergraduate students).", INTL, "fail"),
        ("Graduating by June 2027", NEWGRAD, "pass"),
    ],
)
def test_graduation(text, persona, expected):
    assert gate(run(persona, job(description=text)), "graduation").result == expected


@pytest.mark.parametrize(
    ("text", "persona", "expected"),
    [
        ("- Currently pursuing a Bachelor's degree in CS", INTERN, "pass"),
        (
            "- Currently pursuing a Bachelor's degree in CS",
            INTL,
            "fail",
        ),  # regression: "Bachel-or's"
        ("- Pursuing a BS or MS in Computer Science", INTL, "pass"),
        ("- Currently pursuing an MS or PhD", INTERN, "fail"),
        ("- Currently pursuing a PhD in Computer Science", INTL, "fail"),
        ("- Bachelor's degree or equivalent experience required", SENIOR, "pass_unverified"),
        ("- Master's degree required", SENIOR, "fail"),
    ],
)
def test_degree(text, persona, expected):
    assert gate(run(persona, job(description=f"Requirements\n{text}")), "degree").result == expected


def test_years_of_experience_ignores_preferred_sections():
    must = "Requirements\n- 15+ years of experience building infrastructure"
    pref = "Requirements\n- Go\nNice to have\n- 15+ years of experience building infrastructure"
    assert gate(run(SENIOR, job("Principal Engineer", must)), "years_experience").result == "fail"
    assert (
        gate(run(SENIOR, job("Principal Engineer", pref)), "years_experience").result
        == "pass_unverified"
    )
    assert (
        gate(
            run(SENIOR, job("Senior Engineer", "- 8+ years of backend experience")),
            "years_experience",
        ).result
        == "pass"
    )


def test_language():
    j = job(description="Requirements\n- Fluency in Spanish is required")
    assert gate(run(INTL, j), "language").result == "fail"
    assert (
        gate(run(INTL, job(description="Business-level Mandarin required")), "language").result
        == "pass"
    )
    g = gate(run(INTERN, j), "language")
    assert g.result == "unclear" and g.route == "user"  # no languages listed


def test_preferred_section_is_never_a_knockout():
    j = job(description="Requirements\n- Python\nNice to have\n- U.S. citizenship required")
    assert gate(run(INTL, j), "citizenship").result == "pass_unverified"


def test_overall():
    P = gt.GateResult
    assert gt.overall([P("a", "pass", ""), P("b", "pass_unverified", "")]) == "passed"
    assert gt.overall([P("a", "pass", ""), P("b", "unclear", "")]) == "unclear"
    assert gt.overall([P("a", "unclear", ""), P("b", "fail", "")]) == "failed"


def test_every_failure_has_a_reason():
    for persona in (INTERN, INTL, NEWGRAD, SENIOR):
        for r in run(
            persona, job("Senior Staff Intern, Fall 2025", NO_SPONSOR, locations=["Paris, France"])
        ):
            assert r.reason, r
