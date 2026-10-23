"""Resume drafting from messy text: fragmented PDFs, wrapped bullets, glued years, inline
skill and award lines. Synthetic text only."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jobhunt.onboarding import resume as rs
from tests.conftest import PERSONAS, PERSONAS_DIR
from tests.fixtures.resumes.make_resumes import plain_lines

EMPTY = rs.Contact()


def persona_text(persona: str) -> str:
    md = (PERSONAS_DIR / persona / "resume.md").read_text(encoding="utf-8")
    return "\n".join(plain_lines(md))


@pytest.mark.parametrize("persona", PERSONAS)
def test_one_word_per_line_is_rejoined(persona):
    """What some PDFs extract: every word on its own line. The draft used to have hundreds of
    experience entries; rejoined, it matches the draft from the clean text."""
    clean = persona_text(persona)
    words = "\n".join(clean.split())
    assert rs.fragmented(words) and not rs.fragmented(clean)
    good = rs.draft_profile(clean, EMPTY, "r.pdf")
    fixed = rs.draft_profile(rs.rejoin(words), EMPTY, "r.pdf")
    assert "_warnings" not in fixed
    assert len(fixed["experience"]) == len(good["experience"])
    assert len(fixed["education"]) == len(good["education"])
    for f, g in zip(fixed["education"], good["education"], strict=True):
        # without line breaks a course list's last item can't be told from a school name
        assert f["school"].endswith(g["school"]) and f["graduation"] == g["graduation"]
    for f, g in zip(fixed["experience"], good["experience"], strict=True):
        assert (f["start"], f["end"]) == (g["start"], g["end"])
        assert len(f["bullets"]) == len(g["bullets"])


def test_pdf_falls_back_to_layout_mode_then_rejoins(monkeypatch, tmp_path):
    clean = persona_text("newgrad-analyst")
    words = "\n".join(clean.split())
    calls = []

    class Page:
        def extract_text(self, extraction_mode="plain"):
            calls.append(extraction_mode)
            return clean if extraction_mode == "layout" and layout_ok else words

    import pypdf

    monkeypatch.setattr(pypdf, "PdfReader", lambda p: SimpleNamespace(pages=[Page()]))
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    layout_ok = True
    assert rs.extract_text(pdf).strip() == clean.strip() and calls == ["plain", "layout"]
    layout_ok = False  # layout mode is fragmented too: the words are rejoined
    assert not rs.fragmented(rs.extract_text(pdf))


def test_absurd_sections_are_left_out_with_a_warning():
    text = "Experience\n" + "\n".join(f"Org {i} — Analyst, Jan 2020 – Feb 2020" for i in range(40))
    d = rs.draft_profile(text, EMPTY, "r.pdf")
    assert d["experience"] == []
    assert d["_warnings"] and "40 entries" in d["_warnings"][0]


def test_wrapped_bullets_stay_one_bullet():
    text = """Experience
Harborline Credit Union — Data Analyst Intern, Jun 2026 – Aug 2026
- Built weekly Tableau dashboards of member account openings for the
  marketing team and branch managers
- Wrote SQL queries against the member data warehouse to answer
ad-hoc questions from three departments.
- Automated a monthly report in Python
Coastal Plains University Athletics — Student Analyst, Sep 2025 – present
- Track player workload data in Excel
"""
    exp = rs.draft_profile(text, EMPTY, "r.pdf")["experience"]
    assert len(exp) == 2
    assert exp[0]["bullets"] == [
        "Built weekly Tableau dashboards of member account openings for the marketing team "
        "and branch managers",
        "Wrote SQL queries against the member data warehouse to answer ad-hoc questions from "
        "three departments.",
        "Automated a monthly report in Python",
    ]
    assert exp[1]["org"] == "Coastal Plains University Athletics"


def test_project_metadata_years_are_not_tech():
    text = """Projects
City Bike Demand | Python, Streamlit2026
- Modeled daily bike-share demand
Transit Delay Map — Python, Leaflet | Jan 2025 – Mar 2025
- Mapped bus delays
Grade Tracker | Java, SQLite, 2024
- Tracked grades
"""
    projects = rs.draft_profile(text, EMPTY, "r.pdf")["projects"]
    assert [p["tech"] for p in projects] == [
        ["Python", "Streamlit"],
        ["Python", "Leaflet"],
        ["Java", "SQLite"],
    ]


def test_inline_skill_and_award_lines_are_not_projects():
    text = """Education
Coastal Plains University — B.S. Statistics, expected May 2027
Projects
City Bike Demand — Python, pandas
- Modeled daily bike-share demand
Technical Skills: SQL, Python, R
Languages: English, Spanish
Awards: Dean's List (2025, 2026); Data Challenge Winner
"""
    d = rs.draft_profile(text, EMPTY, "r.pdf")
    assert [p["name"] for p in d["projects"]] == ["City Bike Demand"]
    assert d["skills"]["technical_skills"] == ["SQL", "Python", "R"]
    assert d["skills"]["languages"] == ["English", "Spanish"]
    assert d["education"][0]["honors"] == ["Dean's List (2025, 2026)", "Data Challenge Winner"]


def test_a_header_with_a_colon_still_starts_its_section():
    text = "Technical Skills:\nSQL, Python\nAwards:\nDean's List\n"
    d = rs.draft_profile(text, EMPTY, "r.pdf")
    assert d["skills"]["skills"] == ["SQL", "Python"]
