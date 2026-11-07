"""Setup-form answers code can read from what the user already gave: their resume, their profile
and their preferences. Each suggestion fills only an *empty* field, says where it came from, and
is highlighted on the form; the user checks it and confirms by saving. Nothing is guessed, and
work authorization, citizenship and EEO answers are never suggested.

Everything here is local code over the user's own files; no model sees the resume or the result.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from jobhunt.models import Profile

RESUME, PROFILE, PREFS = "your resume", "your profile", "your preferences"

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT",
    "VA", "WA", "WV", "WI", "WY", "DC", "PR",
}  # fmt: skip
# spoken languages only: a resume's "Languages:" line usually lists programming languages
HUMAN_LANGUAGES = {
    "english", "spanish", "french", "german", "italian", "portuguese", "mandarin", "chinese",
    "cantonese", "japanese", "korean", "hindi", "urdu", "bengali", "punjabi", "gujarati",
    "tamil", "telugu", "marathi", "kannada", "malayalam", "arabic", "hebrew", "persian", "farsi",
    "turkish", "russian", "ukrainian", "polish", "czech", "dutch", "swedish", "norwegian",
    "danish", "finnish", "greek", "romanian", "hungarian", "vietnamese", "thai", "indonesian",
    "malay", "tagalog", "filipino", "swahili", "amharic", "yoruba", "igbo", "asl",
    "american sign language",
}  # fmt: skip
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_GPA = re.compile(r"\bGPA\b\s*[:\-]?\s*([0-4]\.\d{1,3})(?:\s*/\s*([45](?:\.0{1,2})?))?", re.I)
_CITY_ST = re.compile(
    r"(?:^|[|•·,;]\s*|\s{2,})([A-Z][A-Za-z.'-]+(?:\s[A-Z][A-Za-z.'-]+){0,2}),\s*([A-Z]{2})\b"
)
_LANG_LINE = re.compile(r"^\W*(?:spoken\s+)?languages?\s*[:\-–]\s*(.+)$", re.I)
_GRAD_DEGREE = re.compile(r"^(m\.?s|m\.?a|m\.?eng|mba|ph\.?d|master|doctor)", re.I)


@dataclass
class Suggestion:
    path: str  # dotted path in answers.yaml
    value: Any
    source: str  # where it came from, shown on the form


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _get(d: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(part)
    return d


def _pref(prefs: dict[str, Any], path: str) -> Any:
    v = _get(prefs, path)
    return v.get("value") if isinstance(v, dict) and "value" in v else v


def class_standing(graduation: str | None, degree: str | None, today: date) -> str | None:
    """Freshman..Senior from the expected graduation (YYYY-MM) and today; a master's or PhD is
    "Graduate"; a past date is "Graduated"."""
    if not graduation or not (m := re.match(r"(\d{4})-(\d{2})", graduation)):
        return None
    gy, gm = int(m.group(1)), int(m.group(2))
    if (gy, gm) < (today.year, today.month):
        return "Graduated"
    if degree and _GRAD_DEGREE.match(degree.strip()):
        return "Graduate"
    # the academic year ending in June: Nov 2026 is in the year ending 2027
    ends = today.year + (1 if today.month >= 7 else 0)
    left = gy - ends + (1 if gm >= 7 else 0)
    return {0: "Senior", 1: "Junior", 2: "Sophomore", 3: "Freshman"}.get(left)


def _months(start: str | None, end: str | None, today: date) -> int | None:
    def ym(s: str | None) -> tuple[int, int] | None:
        m = re.match(r"(\d{4})-(\d{2})", s or "")
        return (int(m.group(1)), int(m.group(2))) if m else None

    a = ym(start)
    b = ym(end) if end else (today.year, today.month)
    if not a or not b:
        return None
    return max(0, (b[0] - a[0]) * 12 + b[1] - a[1])


def _school_domain(email: str) -> bool:
    """A university address: a domain with an ``edu`` label or ``ac.<country>``."""
    labels = email.rsplit("@", 1)[-1].lower().split(".")
    return "edu" in labels[1:] or any(
        a == "ac" and len(b) == 2 for a, b in itertools.pairwise(labels)
    )


def from_resume_text(text: str, contact_email: str | None) -> dict[str, Any]:
    """What a resume's text states plainly: location, school email, GPA, spoken languages."""
    out: dict[str, Any] = {}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines[:6]:  # the header
        for m in _CITY_ST.finditer(ln):
            if m.group(2) in US_STATES and "@" not in m.group(1):
                out.setdefault("city", m.group(1).strip())
                out.setdefault("region", m.group(2))
    edu = [e for e in _EMAIL.findall(text) if _school_domain(e)]
    edu = [e for e in edu if e.lower() != (contact_email or "").lower()]
    if len(set(edu)) == 1:
        out["school_email"] = edu[0]
    gpas = {(m.group(1), m.group(2)) for m in _GPA.finditer(text)}
    if len(gpas) == 1:  # several GPAs (two degrees): don't pick one
        g, scale = gpas.pop()
        out["gpa"] = float(g)
        if scale:
            out["gpa_scale"] = float(scale)
    langs: list[str] = []
    for i, ln in enumerate(lines):
        if ln.rstrip().endswith(",") and i + 1 < len(lines) and ":" not in lines[i + 1]:
            ln = f"{ln} {lines[i + 1]}"  # a list the layout wrapped
        if m := _LANG_LINE.match(ln):
            for item in re.split(r"[,;/|]", m.group(1)):
                name = re.sub(r"\(.*?\)|[-–:].*$", "", item).strip()
                if name.lower() in HUMAN_LANGUAGES and name not in langs:
                    langs.append(name)
    if langs:
        out["languages"] = langs
    return out


def suggest(
    answers: dict[str, Any] | None,
    prefs: dict[str, Any] | None,
    *,
    profile: Profile | None,
    contact: dict[str, Any] | None,
    resume_text: str | None,
    today: date,
) -> list[Suggestion]:
    """Suggestions for empty answers.yaml fields. Existing answers always win."""
    a, p = answers or {}, prefs or {}
    c = contact or {}
    out: list[Suggestion] = []

    def add(path: str, value: Any, source: str) -> None:
        if not _empty(value) and _empty(_get(a, path)):
            out.append(Suggestion(path, value, source))

    # contact details from the resume header
    name = (c.get("name") or "").split()
    if len(name) >= 2:
        add("identity.legal_first_name", name[0], RESUME)
        add("identity.legal_last_name", " ".join(name[1:]), RESUME)
    add("identity.email", c.get("email"), RESUME)
    add("identity.phone", c.get("phone"), RESUME)
    for kind in ("linkedin", "github", "portfolio"):
        add(f"links.{kind}", (c.get("links") or {}).get(kind), RESUME)
    text = from_resume_text(resume_text or "", c.get("email"))
    add("identity.city", text.get("city"), RESUME)
    add("identity.region", text.get("region"), RESUME)
    if text.get("region"):
        add("identity.country", "US", RESUME)
    add("identity.school_email", text.get("school_email"), RESUME)
    add("education.gpa", text.get("gpa"), RESUME)
    add("education.gpa_scale", text.get("gpa_scale"), RESUME)
    add("skills.spoken_languages", text.get("languages"), RESUME)

    # education and experience from the profile
    if profile and profile.education:
        ed = profile.education[0]
        if m := re.match(r"(\d{4})-(\d{2})", ed.graduation or ""):
            add("education.graduation_year", int(m.group(1)), PROFILE)
            add("education.graduation_month", int(m.group(2)), PROFILE)
        add("education.class_standing", class_standing(ed.graduation, ed.degree, today), PROFILE)
    if profile and profile.experience:
        orgs = sorted({e.org for e in profile.experience if e.org})
        have = list(_get(a, "experience.prior_employers") or [])
        missing = [o for o in orgs if o.casefold() not in {h.casefold() for h in have}]
        if missing:  # adds to the list; what's there stays
            out.append(Suggestion("experience.prior_employers", have + missing, PROFILE))
        months = [_months(e.start, e.end, today) for e in profile.experience]
        if all(m is not None for m in months):
            add("experience.years", round(sum(m or 0 for m in months) / 12 * 2) / 2, PROFILE)

    # availability and relocation the preferences already state
    add("availability.start_date", _pref(p, "timing.earliest_start"), PREFS)
    add("availability.end_date", _pref(p, "timing.latest_end"), PREFS)
    add("availability.terms", _pref(p, "timing.terms"), PREFS)
    add("availability.hours_per_week", _pref(p, "timing.hours_per_week"), PREFS)
    reloc = _pref(p, "location.relocation")
    if isinstance(reloc, bool):
        add("location.willing_to_relocate", "yes" if reloc else "no", PREFS)
    return out
