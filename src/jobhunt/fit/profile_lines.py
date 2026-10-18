"""The user's profile as citable lines, and facts derived from it.

Every claim about the user cites one of these lines by ``ref``; ingest (Milestone 5) verifies the
quoted text against it. The lines are de-identified: no name, contact details, links or school
names (degree and major are kept). Sensitive answers never appear here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from jobhunt.models import Answers, Profile

DEGREE_LEVELS = {"associate": 1, "bachelor": 2, "master": 3, "mba": 3, "doctorate": 4}

_DEGREE_RE = [
    (re.compile(r"\b(ph\.?\s?d|doctor(ate|al)?|d\.?phil)\b", re.I), "doctorate"),
    (re.compile(r"\bmba\b", re.I), "mba"),
    (re.compile(r"\b(m\.?s\.?c?|m\.?eng|m\.?a|master'?s?|ms|meng)\b", re.I), "master"),
    (
        re.compile(
            r"\b(b\.?s\.?c?|b\.?eng|b\.?a|b\.?tech|bachelor'?s?|bs|ba|beng|undergrad\w*)\b", re.I
        ),
        "bachelor",
    ),
    (re.compile(r"\b(a\.?s|a\.?a|associate'?s?)\b", re.I), "associate"),
]


def degree_kind(text: str | None) -> str | None:
    if not text:
        return None
    for rx, kind in _DEGREE_RE:
        if rx.search(text):
            return kind
    return None


@dataclass(frozen=True)
class ProfileLine:
    ref: str
    text: str
    section: str  # headline | education | coursework | skills | experience | project


def _redactor(profile: Profile) -> re.Pattern[str] | None:
    schools = sorted({ed.school.strip() for ed in profile.education if ed.school.strip()}, key=len)
    if not schools:
        return None
    return re.compile("|".join(re.escape(s) for s in reversed(schools)), re.I)


def profile_lines(profile: Profile) -> list[ProfileLine]:
    """Citable lines. School names are replaced with "[school]" wherever they appear (an
    on-campus job names the school too)."""
    rx = _redactor(profile)
    return [
        ProfileLine(ln.ref, rx.sub("[school]", ln.text) if rx else ln.text, ln.section)
        for ln in _raw_lines(profile)
    ]


def _raw_lines(profile: Profile) -> list[ProfileLine]:
    out: list[ProfileLine] = []
    if profile.headline:
        out.append(ProfileLine("headline", profile.headline, "headline"))
    for i, ed in enumerate(profile.education):
        parts = [ed.degree or "Degree"]
        if ed.majors:
            parts.append("in " + ", ".join(ed.majors))
        if ed.minors:
            parts.append("(minor: " + ", ".join(ed.minors) + ")")
        if ed.graduation:
            parts.append(f"graduation {ed.graduation}")
        out.append(ProfileLine(f"education[{i}]", " ".join(parts), "education"))
        if ed.coursework:
            out.append(
                ProfileLine(
                    f"education[{i}].coursework",
                    "Coursework: " + ", ".join(ed.coursework),
                    "coursework",
                )
            )
        for j, h in enumerate(ed.honors):
            out.append(ProfileLine(f"education[{i}].honors[{j}]", h, "education"))
    for cat, skills in profile.skills.items():
        if skills:
            label = cat.replace("_", " ").capitalize()
            out.append(ProfileLine(f"skills.{cat}", f"{label}: {', '.join(skills)}", "skills"))
    for i, ex in enumerate(profile.experience):
        span = f"{ex.start or '?'} – {ex.end or 'present'}"
        out.append(
            ProfileLine(f"experience[{i}]", f"{ex.title} at {ex.org} ({span})", "experience")
        )
        for j, b in enumerate(ex.bullets):
            out.append(ProfileLine(f"experience[{i}].bullets[{j}]", b, "experience"))
    for i, pr in enumerate(profile.projects):
        out.append(ProfileLine(f"projects[{i}]", f"{pr.name}: {pr.summary}", "project"))
        if pr.tech:
            out.append(ProfileLine(f"projects[{i}].tech", "Tech: " + ", ".join(pr.tech), "project"))
        for j, b in enumerate(pr.bullets):
            out.append(ProfileLine(f"projects[{i}].bullets[{j}]", b, "project"))
    return out


def deidentified_resume(profile: Profile) -> str:
    return "\n".join(line.text for line in profile_lines(profile))


# --- derived facts --------------------------------------------------------------------------


def _ym(value: str | None) -> date | None:
    if not value:
        return None
    m = re.match(r"(\d{4})(?:-(\d{1,2}))?", str(value))
    if not m:
        return None
    return date(int(m.group(1)), int(m.group(2) or 1), 1)


@dataclass(frozen=True)
class Education:
    kind: str | None  # bachelor | master | ...
    graduation: date | None
    in_progress: bool
    majors: tuple[str, ...]


def education_facts(profile: Profile, today: date) -> list[Education]:
    out = []
    for ed in profile.education:
        grad = _ym(ed.graduation)
        out.append(
            Education(
                kind=degree_kind(ed.degree),
                graduation=grad,
                in_progress=grad is None or grad > today,
                majors=tuple(ed.majors),
            )
        )
    return out


def highest_completed_level(edu: list[Education]) -> int:
    return max((DEGREE_LEVELS.get(e.kind or "", 0) for e in edu if not e.in_progress), default=0)


def current_degrees(edu: list[Education]) -> list[Education]:
    return [e for e in edu if e.in_progress]


def expected_graduation(profile: Profile, answers: Answers, today: date) -> date | None:
    """The answers form wins; otherwise the latest in-progress degree in the profile."""
    a = answers.education
    if a.graduation_year:
        return date(a.graduation_year, a.graduation_month or 6, 1)
    current = [
        e.graduation for e in current_degrees(education_facts(profile, today)) if e.graduation
    ]
    return max(current) if current else None


def years_of_experience(profile: Profile, answers: Answers, today: date) -> float:
    """The answers form wins; otherwise merged (non-overlapping) spans of profile experience."""
    if answers.experience.years is not None:
        return float(answers.experience.years)
    spans = []
    for ex in profile.experience:
        start = _ym(ex.start)
        if start is None:
            continue
        end = _ym(ex.end) or today
        if end > start:
            spans.append((start, end))
    spans.sort()
    total, cur_s, cur_e = 0.0, None, None
    for s, e in spans:
        if cur_e is None or s > cur_e:
            if cur_e is not None and cur_s is not None:
                total += (cur_e - cur_s).days
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None and cur_s is not None:
        total += (cur_e - cur_s).days
    return round(total / 365.25, 1)
