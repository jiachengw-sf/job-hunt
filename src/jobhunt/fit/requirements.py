"""Stage 2 (code part): split a posting into requirement items and pre-match obvious ones.

Every requirement's text is a verbatim slice of the posting. Pre-matching only marks a
requirement ``met``/``stated`` when a profile line literally contains the skill (or a curated
synonym); everything else is left for the judgment task.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache
from typing import Literal

import yaml

from jobhunt.fit.profile_lines import ProfileLine
from jobhunt.paths import package_file

Importance = Literal["must", "preferred"]

_H = r"^\W*(?:{})\W*$"
MUST_HEADERS = re.compile(
    _H.format(
        r"(?:basic|minimum|required|key|core)?\s*(?:qualifications?|requirements?|skills?(?: "
        r"(?:and|&) "
        r"experience)?)|what (?:you(?:'ll| will)? need|we(?:'re| are) looking for|you bring|"
        r"we expect|you have)|who you are|you (?:have|should have|might be a fit if)|"
        r"must[- ]haves?|about you|your (?:background|experience|skills)|"
        r"(?:you'll|you will) (?:need|have)|requirements? (?:and|&) qualifications?"
    ),
    re.I,
)
PREFERRED_HEADERS = re.compile(
    _H.format(
        r"(?:preferred|desired|additional|bonus|nice[- ]to[- ]have)\s*(?:qualifications?|skills?|"
        r"experience|requirements?)?|nice[- ]to[- ]haves?|bonus(?: points)?|pluses|"
        r"(?:it's |it is )?a plus(?: if you have)?|extra credit|ideally(?:, you have)?|"
        r"we(?:'d| would) love (?:it )?if you (?:have|had)|even better if you have|"
        r"(?:you )?stand out if you have"
    ),
    re.I,
)
OTHER_HEADERS = re.compile(
    _H.format(
        r"(?:key )?responsibilities|what you(?:'ll| will) do|the role|about (?:the |this )?"
        r"(?:role|team|us|company|position|job)|benefits?|perks(?: (?:and|&) benefits)?|"
        r"compensation|pay(?: range)?|salary(?: range)?|location|how to apply|"
        r"equal (?:employment )?opportunity|eeo|who we are|what we offer|why (?:join|work)\b.*|"
        r"our (?:values|mission|benefits)|the team|day[- ]to[- ]day|in this role,? you will|"
        r"you will|your impact|what's in it for you"
    ),
    re.I,
)
INLINE_HEADER = re.compile(
    r"^\W*(requirements|qualifications|preferred|nice to have|bonus)\s*:\s*(.+)$", re.I
)
_BULLET = re.compile(r"^[\s•●▪◦‣·*\-–—]+")
_SENTENCE = re.compile(r"(?<=[.;])\s+(?=[A-Z])")
MAX_ITEM_CHARS = 400


@dataclass
class Requirement:
    seq: int
    text: str  # verbatim slice of the posting
    importance: Importance
    type: str  # skill | experience | degree | eligibility | location | other
    section: str
    status: str | None = None  # met | partial | unmet (None = not assessed)
    tier: str | None = None  # stated | inferred | none
    evidence: str | None = None  # profile line text (verbatim) when stated
    evidence_ref: str | None = None
    skills: list[str] = field(default_factory=list)


@dataclass
class Extraction:
    requirements: list[Requirement]
    messy: bool  # a judgment task should (re)extract
    reason: str | None = None


def _header_kind(line: str) -> str | None:
    s = line.strip()
    if len(s) > 70:
        return None
    if PREFERRED_HEADERS.match(s):
        return "preferred"
    if MUST_HEADERS.match(s):
        return "must"
    if OTHER_HEADERS.match(s):
        return "other"
    return None


def _items(line: str) -> list[str]:
    """Split one line into items (sentences when it is a long paragraph). Verbatim slices."""
    body = _BULLET.sub("", line).strip()
    if len(body) <= MAX_ITEM_CHARS:
        return [body] if body else []
    return [s.strip() for s in _SENTENCE.split(body) if s.strip()]


@dataclass
class Section:
    kind: str  # must | preferred | other | intro
    header: str | None
    lines: list[str]


def split_sections(description: str) -> list[Section]:
    sections = [Section("intro", None, [])]
    for raw in description.splitlines():
        line = raw.strip()
        if not line:
            continue
        kind = _header_kind(line)
        if kind:
            sections.append(Section(kind, line, []))
            continue
        m = INLINE_HEADER.match(line)
        if m:
            k = "preferred" if re.match(r"preferred|nice|bonus", m.group(1), re.I) else "must"
            sections.append(Section(k, m.group(1), [m.group(2).strip()]))
            continue
        sections[-1].lines.append(line)
    return sections


_TYPE_RULES = [
    ("degree", re.compile(r"\b(degree|bachelor|master|ph\.?d|b\.?s\.?|m\.?s\.?|majoring|major in|"
                          r"pursuing|enrolled|coursework|gpa)\b", re.I)),
    ("eligibility", re.compile(r"authori[sz]|citizen|clearance|sponsor|\bvisa\b|eligible to work|"
                               r"graduat|class of|rising (sophomore|junior|senior)|background "
                               r"check|"
                               r"18 years|legally", re.I)),
    ("experience", re.compile(r"\b\d+\+?\s*(-|to|–)?\s*\d*\+?\s*years?\b|\bexperience\b|track record|"  # noqa: E501
                              r"proven|background in", re.I)),
    ("location", re.compile(r"relocat|on-?site|in[- ]office|hybrid|\bremote\b|commut|based in|"
                            r"located in|\btravel\b", re.I)),
]  # fmt: skip


def classify(text: str, skills: list[str]) -> str:
    for kind, rx in _TYPE_RULES:
        if rx.search(text):
            if kind == "experience" and skills and not re.search(r"\d+\+?\s*years?", text, re.I):
                return "skill"
            return kind
    return "skill" if skills else "other"


def extract(description: str) -> Extraction:
    if not description.strip():
        return Extraction([], messy=False, reason="no posting text")
    sections = split_sections(description)
    reqs: list[Requirement] = []
    for sec in sections:
        if sec.kind not in ("must", "preferred"):
            continue
        for line in sec.lines:
            for item in _items(line):
                if not re.search(r"[A-Za-z]{2}", item):
                    continue
                skills = find_skills(item)
                reqs.append(
                    Requirement(
                        seq=len(reqs),
                        text=item,
                        importance="must" if sec.kind == "must" else "preferred",
                        type=classify(item, skills),
                        section=sec.header or sec.kind,
                        skills=skills,
                    )
                )
    if not reqs:
        messy = len(description) >= 200
        return Extraction(
            [], messy=messy, reason="no requirement sections found" if messy else None
        )
    long_items = sum(1 for r in reqs if len(r.text) > 250)
    return Extraction(
        reqs,
        messy=long_items > len(reqs) / 2,
        reason="requirement sections are long paragraphs" if long_items > len(reqs) / 2 else None,
    )


# --- skills and synonyms --------------------------------------------------------------------


@cache
def synonyms() -> dict[str, list[str]]:
    """canonical skill -> aliases (case-insensitive, whole-word), from defaults/synonyms.yaml."""
    data = yaml.safe_load(package_file("defaults", "synonyms.yaml").read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for canon, aliases in (data or {}).items():
        aliases = [str(a) for a in aliases or []]
        exact = {a[1:] for a in aliases if a.startswith("=")}
        out[str(canon)] = ([] if str(canon) in exact else [str(canon)]) + aliases
    return out


@cache
def _alias_patterns() -> list[tuple[str, re.Pattern[str]]]:
    pats = []
    for canon, aliases in synonyms().items():
        alts = sorted({a for a in aliases if not a.startswith("=")}, key=len, reverse=True)
        exact = [a[1:] for a in aliases if a.startswith("=")]  # "=Go": case-sensitive alias
        parts = [rf"(?i:{re.escape(a)})" for a in alts] + [re.escape(a) for a in exact]
        alt = "|".join(f"(?:{p})" for p in parts)
        pats.append((canon, re.compile(rf"(?<![\w+#.&'-])(?:{alt})(?![\w+#&'-])")))
    return pats


def find_skills(text: str) -> list[str]:
    return [canon for canon, rx in _alias_patterns() if rx.search(text)]


def _line_skills(lines: list[ProfileLine]) -> dict[str, list[ProfileLine]]:
    out: dict[str, list[ProfileLine]] = {}
    for line in lines:
        for s in find_skills(line.text):
            out.setdefault(s, []).append(line)
    return out


_EVIDENCE_PRIORITY = {"experience": 0, "project": 1, "skills": 2, "coursework": 3}


def prematch(reqs: list[Requirement], lines: list[ProfileLine]) -> None:
    """Mark skill requirements whose every named skill appears in a profile line. A requirement
    naming several skills where only some appear is ``partial``. Nothing else is touched."""
    have = _line_skills(lines)
    for r in reqs:
        if r.type in ("eligibility", "location") or not r.skills:
            continue
        if r.type == "experience" and re.search(r"\d+\+?\s*years?", r.text, re.I):
            continue  # years of experience need judgment
        found = [s for s in r.skills if s in have]
        if not found:
            continue
        best = sorted(
            (ln for s in found for ln in have[s]),
            key=lambda ln: (_EVIDENCE_PRIORITY.get(ln.section, 9), ln.ref),
        )[0]
        r.tier, r.evidence, r.evidence_ref = "stated", best.text, best.ref
        if len(found) == len(r.skills):
            r.status = "met"
        elif _is_any_of(r.text):
            r.status = "met"  # "Python, Java, or Go": one is enough
        else:
            r.status = "partial"


def _is_any_of(text: str) -> bool:
    return bool(re.search(r"\b(or|one of|any of|at least one|such as|e\.g\.|like)\b", text, re.I))


# --- years of experience in a posting ----------------------------------------------------------

YEARS_RE = re.compile(
    r"(?<![\d.])(\d{1,2})\s*\+?\s*(?:(?:-|to|–)\s*(\d{1,2})\s*\+?\s*)?(?:years?|yrs?)(?:'|’)?\s+"
    r"(?:of\s+)?(?:[\w/+#.-]+\s+){0,4}?(?:experience|exp\b|work(?:ing)?\b|building|developing)",
    re.I,
)


def required_years(description: str) -> tuple[float | None, str | None]:
    """Largest minimum years of experience asked for outside preferred/bonus sections."""
    best: tuple[float, str] | None = None
    for sec in split_sections(description):
        if sec.kind in ("preferred", "other"):
            continue
        for line in sec.lines:
            for m in YEARS_RE.finditer(line):
                n = float(m.group(1))
                if n > 25:
                    continue
                if best is None or n > best[0]:
                    best = (n, line)
    return (best[0], best[1]) if best else (None, None)
