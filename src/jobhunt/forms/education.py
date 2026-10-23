"""Education blocks: a form section that asks for school, degree, discipline and dates on its
own (Greenhouse's Education block), and matching profile entries to its options.

The profile lists education most recent first. The first entry answers the block the page
shows; each further entry goes into an "Add another" copy, which is always optional. A choice is
made only when it is unambiguous: a degree option of the same level ("BS" -> "Bachelor's
Degree"), a discipline or school whose name equals the profile's (case, punctuation, "&"/"and"
and a leading "The" aside). Anything else is left for the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jobhunt.forms.model import EducationBlock, FieldType, FormField
from jobhunt.models.profile import Education

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]  # fmt: skip


@dataclass(frozen=True)
class Part:
    name: str  # the ATS's own name for it (Greenhouse's education_config keys)
    dom: str  # the page's id stem: ``<dom>--<entry>``
    label: str
    key: str  # the answer-bank key the first entry resolves through
    type: FieldType


PARTS = (
    Part("school_name", "school", "School", "education.school", "select"),
    Part("degree", "degree", "Degree", "education.degree", "select"),
    Part("discipline", "discipline", "Discipline", "education.major", "select"),
    Part("start_month", "start-month", "Start date month", "education.start_month", "select"),
    Part("start_year", "start-year", "Start date year", "education.start_year", "number"),
    Part("end_month", "end-month", "End date month", "education.end_month", "select"),
    Part("end_year", "end-year", "End date year", "education.end_year", "number"),
)
BY_DOM = {p.dom: p for p in PARTS}
_ID = re.compile(r"^([a-z-]+)--(\d+)$")


def part_of(field_id: str) -> tuple[Part, int] | None:
    """The block part and entry index a field id names (``degree--1`` -> degree, 1)."""
    m = _ID.match(field_id)
    if not m or m.group(1) not in BY_DOM:
        return None
    return BY_DOM[m.group(1)], int(m.group(2))


def fields(block: EducationBlock, entry: int) -> list[FormField]:
    """The form fields of one entry. Only the first entry's can be required."""
    out = []
    for p in PARTS:
        if p.name not in block.parts:
            continue
        options = {"degree": block.degrees, "discipline": block.disciplines}.get(p.name, [])
        if p.name.endswith("_month"):
            options = list(MONTHS)
        out.append(
            FormField(
                id=f"{p.dom}--{entry}",
                label=p.label if entry == 0 else f"{p.label} (education {entry + 1})",
                type=p.type,
                required=block.parts[p.name] and entry == 0,
                options=list(options),
            )
        )
    return out


# --- values from a profile entry -------------------------------------------------------------


def month_name(ym: str | None) -> str | None:
    """ "2028-05" -> "May"."""
    m = re.match(r"^\d{4}-(\d{2})", ym or "")
    return MONTHS[int(m.group(1)) - 1] if m and 1 <= int(m.group(1)) <= 12 else None


def year_of(ym: str | None) -> str | None:
    m = re.match(r"^(\d{4})", ym or "")
    return m.group(1) if m else None


def raw_value(part: Part, e: Education) -> str | None:
    """What the profile entry says for one part, before matching it to the form's options."""
    return {
        "school_name": e.school,
        "degree": e.degree,
        "discipline": ", ".join(e.majors) or None,
        "start_month": month_name(e.start),
        "start_year": year_of(e.start),
        "end_month": month_name(e.graduation),
        "end_year": year_of(e.graduation),
    }[part.name]


def pick(part: Part, value: str, options: list[str]) -> str | None:
    """The option this value confidently means, or None."""
    if part.name == "degree":
        return pick_degree(value, options)
    if part.name in ("school_name", "discipline"):
        return pick_name(value, options)
    want = value.casefold()
    return next((o for o in options if o.strip().casefold() == want), None)


# --- matching --------------------------------------------------------------------------------

_LEVELS: list[tuple[str, re.Pattern[str]]] = [
    ("mba", re.compile(r"\bm ?b ?a\b|\bmasters? of business administration\b")),
    ("jd", re.compile(r"\bj ?d\b|\bjuris doctor\b")),
    ("md", re.compile(r"\bm ?d\b|\bdoctor of medicine\b")),
    ("phd", re.compile(r"\bph ?d\b|\bd ?phil\b|\bdoctor of philosophy\b")),
    ("doctorate", re.compile(r"\bdoctorate\b|\bdoctoral\b")),
    ("master", re.compile(
        r"\bmasters?\b|\bm ?(s|sc|a|eng|e|asc|fa|phil|math|res|st|cs|ps|pp|pa|ed|arch|fin|ba)\b"
    )),
    ("bachelor", re.compile(
        r"\bbachelors?\b|\bb ?(s|sc|a|eng|e|asc|fa|com|tech|math|arch|ba|ed|bus|cs|mus|des)\b|"
        r"\bundergraduate\b"
    )),
    ("associate", re.compile(r"\bassociates?\b|\ba ?(a|s|as|aa|a ?s)\b")),
    ("high_school", re.compile(r"\bhigh school\b|\bsecondary school\b|\bged\b")),
]  # fmt: skip
# options that name a level but mean something narrower: never chosen for a plain degree
_QUALIFIED = re.compile(
    r"\b(accelerated|advanced|certificate|non degree|post ?doctoral|postdoc|community|technical|"
    r"engineer s degree|diploma|in progress|some)\b"
)


def _plain(s: str) -> str:
    s = s.casefold().replace("&", " and ").replace("’", "'")
    s = re.sub(r"'s?\b", " s", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def degree_level(text: str | None) -> str | None:
    """bachelor | master | mba | phd | doctorate | jd | md | associate | high_school, or None."""
    t = re.sub(r" s\b", "s", _plain(text or ""))  # "master s" -> "masters"
    t = re.sub(r"\b([a-z]) (?=[a-z]\b)", r"\1", t)  # "b s" -> "bs", "ph d" -> "phd"
    for level, rx in _LEVELS:
        if rx.search(t):
            return level
    return None


def pick_degree(value: str, options: list[str]) -> str | None:
    """The option of the same degree level as ``value``. When the form lists several ways to
    say it ("Bachelors", "Bachelor's Degree"), they mean the same: the one saying "degree"
    wins, else the first. A PhD may take a generic "Doctorate" and an MBA a "Master's"."""
    exact = pick_name(value, options)
    if exact:
        return exact
    level = degree_level(value)
    if level is None:
        return None
    fallbacks = {"phd": "doctorate", "mba": "master"}
    for want in (level, fallbacks.get(level)):
        if want is None:
            continue
        hits = [o for o in options if degree_level(o) == want and not _QUALIFIED.search(_plain(o))]
        if hits:
            return next((o for o in hits if "degree" in _plain(o)), hits[0])
    return None


_ABBREV = {
    "cs": "computer science",
    "comp sci": "computer science",
    "ee": "electrical engineering",
    "ece": "electrical and computer engineering",
    "me": "mechanical engineering",
    "math": "mathematics",
    "maths": "mathematics",
    "stats": "statistics",
    "econ": "economics",
}


def _name_key(s: str) -> str:
    t = _plain(s)
    t = re.sub(r"^the ", "", t)
    return _ABBREV.get(t, t)


def pick_name(value: str, options: list[str]) -> str | None:
    """The option naming the same thing as ``value`` (or one of its comma-separated parts, in
    order: a double major is answered by whichever the form lists)."""
    by_key: dict[str, str] = {}
    for o in options:
        by_key.setdefault(_name_key(o), o)
    for v in [value, *(p for p in value.split(",") if p.strip() != value)]:
        if v.strip() and (hit := by_key.get(_name_key(v))):
            return hit
    return None
