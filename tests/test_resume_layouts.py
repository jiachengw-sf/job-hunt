"""Resumes in many layouts (tests/fixtures/resumes/layouts, synthetic people): PDF from Chrome,
LaTeX-style PDF, two-column PDF, an image-only scan, DOCX with a table layout or the contact in
the page header, Markdown. Each must yield what ``expected.json`` says, and the setup form's
suggestions must carry it through."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from jobhunt.models import Profile
from jobhunt.onboarding.ocr import fix_ocr, unavailable
from jobhunt.onboarding.resume import (
    draft_profile,
    extract_text,
    find_contact,
    normalize_text,
    readability,
    title_org,
)
from jobhunt.onboarding.suggest import class_standing, from_resume_text, suggest

D = Path(__file__).parent / "fixtures" / "resumes" / "layouts"
EXP = json.loads((D / "expected.json").read_text(encoding="utf-8"))
FILES = sorted(EXP["files"])


def read(name: str) -> dict:
    path = D / name
    text = extract_text(path)
    c = find_contact(text)
    draft = draft_profile(text, c, name)
    prof = Profile.model_validate({k: v for k, v in draft.items() if k in Profile.model_fields})
    rt = from_resume_text(text, c.email)
    strip = lambda u: (u or "").removeprefix("https://").removeprefix("www.") or None  # noqa: E731
    return {
        "name": c.name, "email": c.email,
        "phone": re.sub(r"\D", "", c.phone or "")[-10:] or None,
        "linkedin": strip(c.links.get("linkedin")), "github": strip(c.links.get("github")),
        "city": rt.get("city"), "region": rt.get("region"), "school_email": rt.get("school_email"),
        "gpa": rt.get("gpa"), "gpa_scale": rt.get("gpa_scale"),
        "school": prof.education[0].school if prof.education else None,
        "graduation": prof.education[0].graduation if prof.education else None,
        "employers": sorted({x.org for x in prof.experience}),
        "spoken_languages": sorted(rt.get("languages") or []),
        "_profile": prof, "_contact": c, "_text": text,
    }  # fmt: skip


@pytest.mark.parametrize("name", FILES)
def test_every_layout_reads_like_the_truth(name):
    if "scanned" in name and (why := unavailable()):
        pytest.skip(f"OCR not available: {why}")
    got = read(name)
    want = EXP["people"][EXP["files"][name]["person"]]
    for k, w in want.items():
        g = got[k]
        if isinstance(w, list):
            assert sorted(w) == g, (name, k)
        else:
            assert w == g, (name, k, g)


@pytest.mark.parametrize("name", [f for f in FILES if "scanned" not in f])
def test_titles_dates_and_degrees(name):
    prof = read(name)["_profile"]
    who = EXP["files"][name]["person"]
    if who == "avery":
        assert [(x.title, x.start, x.end) for x in prof.experience] == [
            ("Software Engineering Intern", "2025-06", "2025-08"),
            ("Teaching Assistant", "2025-01", None),
        ]
        assert [(e.degree, e.majors) for e in prof.education] == [("BS", ["Computer Science"])]
    else:
        assert [(e.school, e.degree, e.majors, e.graduation) for e in prof.education] == [
            ("Cascade Institute of Technology", "MS", ["Data Science"], "2026-12"),
            ("Riverbend College", "BA", ["Economics"], "2023-05"),
        ]


def test_a_scan_without_ocr_says_how_to_read_it(monkeypatch):
    import jobhunt.onboarding.ocr as ocr

    monkeypatch.setattr(ocr, "unavailable", lambda: "the tesseract program isn't installed")
    from jobhunt.errors import ValidationFailed

    with pytest.raises(ValidationFailed, match="install OCR"):
        extract_text(D / "avery-pdf_scanned.pdf")


def test_setup_form_suggestions_from_a_layout_resume():
    got = read("avery-pdf_latex.pdf")
    c = got["_contact"]
    contact = {"name": c.name, "email": c.email, "phone": c.phone, "links": c.links}
    sg = {
        s.path: s.value
        for s in suggest(
            None,
            None,
            profile=got["_profile"],
            contact=contact,
            resume_text=got["_text"],
            today=date(2026, 11, 6),
        )
    }
    assert sg["identity.legal_first_name"] == "Avery"
    assert (sg["identity.city"], sg["identity.region"], sg["identity.country"]) == (
        "Evanston", "IL", "US",
    )  # fmt: skip
    assert sg["identity.school_email"] == "avery@northfield.edu.example"
    assert (sg["education.gpa"], sg["education.gpa_scale"]) == (3.82, 4.0)
    assert sg["education.class_standing"] == "Junior"
    assert sg["skills.spoken_languages"] == ["English", "Spanish", "Mandarin"]
    assert sg["experience.prior_employers"] == [
        "Lakeshore Logistics",
        "Northfield State University",
    ]
    # nothing sensitive is ever suggested
    assert not [p for p in sg if p.startswith(("work_authorization", "eeo"))]


def test_existing_answers_win_over_suggestions():
    got = read("jordan-md.md")
    out = suggest({"identity": {"city": "Tacoma"}}, None, profile=got["_profile"], contact=None,
                  resume_text=got["_text"], today=date(2026, 11, 6))  # fmt: skip
    sg = {s.path for s in out}
    assert "identity.city" not in sg and "identity.region" in sg


@pytest.mark.parametrize(
    ("grad", "degree", "today", "want"),
    [
        ("2030-05", "BS", date(2026, 11, 6), "Freshman"),
        ("2028-05", "BS", date(2026, 11, 6), "Junior"),
        ("2027-05", "BS", date(2026, 11, 6), "Senior"),
        ("2026-12", "MS", date(2026, 11, 6), "Graduate"),
        ("2025-05", "BS", date(2026, 11, 6), "Graduated"),
        ("2027-12", "BS", date(2026, 11, 6), "Junior"),  # graduates mid next year
    ],
)
def test_class_standing(grad, degree, today, want):
    assert class_standing(grad, degree, today) == want


def test_text_normalization():
    assert normalize_text("E D U C A T I O N") == "EDUCATION"
    assert normalize_text("A VERY  T ESTWELL") == "AVERY TESTWELL"
    assert normalize_text("Northﬁeld oﬃce") == "Northfield office"
    assert normalize_text("linkedin.com/in/avery-\nexample") == "linkedin.com/in/avery-example"
    assert readability("") == 0
    assert fix_ocr("Cascade — N.5. in Data Science") == "Cascade — M.S. in Data Science"


@pytest.mark.parametrize(
    ("head", "org", "title"),
    [
        ("Software Engineering Intern, Lakeshore Logistics — Chicago, IL", "Lakeshore Logistics",
         "Software Engineering Intern"),
        ("Lakeshore Logistics — Software Engineering Intern, Jun 2025 – Aug 2025",
         "Lakeshore Logistics", "Software Engineering Intern"),
        ("Data Analyst Jul 2023 – Aug 2025 | Harborview Analytics Seattle, WA",
         "Harborview Analytics", "Data Analyst"),
        ("Research Assistant at Lab of Things, New York, NY", "Lab of Things",
         "Research Assistant"),
    ],
)  # fmt: skip
def test_title_org(head, org, title):
    assert title_org(head) == (org, title)
