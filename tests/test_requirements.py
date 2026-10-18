from __future__ import annotations

from jobhunt.fit.profile_lines import deidentified_resume, profile_lines, years_of_experience
from jobhunt.fit.requirements import extract, find_skills, prematch, required_years
from tests.fit_helpers import TODAY, persona_ctx

POSTING = """About the role
You will build things.
Requirements:
- Currently pursuing a BS in Computer Science, graduating between Dec 2027 and Jun 2028
- Proficiency in Python or Java
- SQL
- Experience with Go and Rust
- 2+ years of experience building web services
Nice to have
- Experience with Kubernetes
Benefits
- Free lunch and a 401(k)
"""


def test_sections_importance_and_verbatim_text():
    ex = extract(POSTING)
    texts = [r.text for r in ex.requirements]
    assert all(t in POSTING for t in texts), "every requirement must be a verbatim slice"
    assert "Free lunch and a 401(k)" not in texts
    by = {r.text: r for r in ex.requirements}
    assert by["SQL"].importance == "must"  # single-word items are kept
    assert by["Experience with Kubernetes"].importance == "preferred"
    assert by["Proficiency in Python or Java"].type == "skill"
    assert by["2+ years of experience building web services"].type == "experience"
    assert (
        by[
            "Currently pursuing a BS in Computer Science, graduating between Dec 2027 and Jun 2028"
        ].type
        == "degree"
    )
    assert not ex.messy


def test_no_sections_is_messy_for_a_judgment_task():
    ex = extract("We are a fast-growing team. " * 20)
    assert ex.requirements == [] and ex.messy


def test_find_skills_synonyms_and_case_sensitive_aliases():
    assert find_skills("Experience with golang and Postgres") == ["Go", "PostgreSQL"]
    assert "Go" not in find_skills("you will go to the office")
    assert find_skills("Send your CV") == []
    assert find_skills("Spring 2027 internship") == []
    assert "R" not in find_skills("our R&D team")


def test_prematch_cites_a_real_profile_line():
    ctx = persona_ctx("first-year-intern")
    lines = profile_lines(ctx.profile)
    ex = extract(POSTING)
    prematch(ex.requirements, lines)
    by = {r.text: r for r in ex.requirements}
    py = by["Proficiency in Python or Java"]
    assert py.status == "met" and py.tier == "stated"
    assert py.evidence in {ln.text for ln in lines}
    assert py.evidence_ref in {ln.ref for ln in lines}
    assert by["Experience with Go and Rust"].status is None  # nothing found: left for judgment
    assert by["2+ years of experience building web services"].status is None  # years need judgment


def test_prematch_partial_when_only_some_skills_found():
    ctx = persona_ctx("intl-sponsorship")
    ex = extract("Requirements\n- Experience with Go and Rust")
    prematch(ex.requirements, profile_lines(ctx.profile))
    assert ex.requirements[0].status == "partial"


def test_required_years_ignores_preferred():
    assert (
        required_years(
            "Requirements\n- 3+ years of Python experience\nNice to have\n- 10+ years of experience"
        )[0]
        == 3
    )


def test_profile_lines_are_deidentified():
    for persona in ("first-year-intern", "intl-sponsorship", "newgrad-analyst", "senior-backend"):
        ctx = persona_ctx(persona)
        text = deidentified_resume(ctx.profile)
        for ed in ctx.profile.education:
            assert ed.school not in text
        for link in ctx.profile.links:
            assert link.url not in text
        ident = ctx.answers.identity
        for v in (ident.email, ident.phone, ident.legal_last_name):
            if v:
                assert v not in text


def test_years_of_experience():
    assert (
        years_of_experience(
            persona_ctx("senior-backend").profile, persona_ctx("senior-backend").answers, TODAY
        )
        == 10
    )
    ctx = persona_ctx("first-year-intern")
    ctx.answers.experience.years = None
    assert 0 < years_of_experience(ctx.profile, ctx.answers, TODAY) < 1
