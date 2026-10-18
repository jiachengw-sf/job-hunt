"""Resume text extraction and a first-draft profile.

Code extracts the text (PDF, DOCX, Markdown or plain text) and splits it into a draft
profile.yaml by section headers. The draft is starting material: the jobhunt-setup skill tidies
it from the *redacted* text (contact lines removed), and the user confirms it before it becomes
profile.yaml. Contact details found on the resume go only to the local form's pre-fill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobhunt.errors import ValidationFailed

SUPPORTED_SUFFIXES = (".pdf", ".docx", ".md", ".txt")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")
_URL = re.compile(
    r"(?:https?://)?(?:www\.)?[a-z0-9-]{2,}(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:/[\w./%-]*)?"
)
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE = rf"(?:{_MON}\s+\d{{4}}|\d{{1,2}}/\d{{4}}|\d{{4}})"
_RANGE = re.compile(
    rf"(?P<start>{_DATE})\s*(?:–|—|-|to)\s*(?P<end>{_DATE}|present|current|now)", re.I
)
_SINGLE = re.compile(rf"(?:expected\s+|anticipated\s+)?(?P<d>{_DATE})\b", re.I)
_DEGREE = re.compile(
    r"\b(B\.?S\.?|B\.?A\.?|B\.?Eng\.?|M\.?S\.?|M\.?A\.?|M\.?Eng\.?|MBA|Ph\.?D\.?|"
    r"Bachelor(?:'s)?(?: of [A-Z][a-z]+)?|Master(?:'s)?(?: of [A-Z][a-z]+)?|Associate(?:'s)?)\b"
)
_SECTIONS = {
    "education": "education",
    "experience": "experience",
    "work experience": "experience",
    "professional experience": "experience",
    "employment": "experience",
    "projects": "projects",
    "personal projects": "projects",
    "selected projects": "projects",
    "skills": "skills",
    "technical skills": "skills",
    "coursework": "coursework",
    "relevant coursework": "coursework",
    "honors": "honors",
    "awards": "honors",
    "honors and awards": "honors",
    "links": "links",
    "summary": "summary",
    "profile": "summary",
    "objective": "summary",
}
_BULLET = re.compile(r"^\s*(?:[-•*▪●◦]|\d+\.)\s+")


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValidationFailed(
            f"{path.name}: unsupported resume format; use PDF, DOCX, Markdown or plain text."
        )
    if suffix == ".pdf":
        from pypdf import PdfReader

        try:
            reader = PdfReader(str(path))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as e:
            raise ValidationFailed(f"{path.name}: couldn't read this PDF ({e}).") from e
    elif suffix == ".docx":
        import docx

        try:
            d = docx.Document(str(path))
        except Exception as e:
            raise ValidationFailed(f"{path.name}: couldn't read this DOCX ({e}).") from e
        lines = [
            f"- {p.text}" if "list" in (p.style.name if p.style else "").lower() else p.text
            for p in d.paragraphs
        ]
        for table in d.tables:
            for row in table.rows:
                lines.append(" | ".join(c.text for c in row.cells))
        text = "\n".join(lines)
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    text = text.replace("\r", "").replace(" ", " ")
    if not text.strip():
        raise ValidationFailed(
            f"{path.name}: no text found (a scanned image?). Export a text PDF or DOCX instead."
        )
    return text


@dataclass
class Contact:
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    links: dict[str, str] = field(default_factory=dict)  # linkedin/github/portfolio -> url


def _clean(line: str) -> str:
    line = re.sub(r"^#+\s*", "", line)
    return re.sub(r"\*\*|__", "", line).strip()


def _looks_like_name(line: str) -> bool:
    words = line.split()
    return (
        2 <= len(words) <= 4
        and all(w[:1].isupper() for w in words)
        and not any(ch.isdigit() or ch in "@|/:" for ch in line)
        and line.lower() not in _SECTIONS
    )


def _url_kind(u: str) -> str:
    low = u.lower()
    if "linkedin.com" in low:
        return "linkedin"
    if "github.com" in low:
        return "github"
    return "portfolio"


def find_contact(text: str) -> Contact:
    c = Contact()
    lines = [_clean(ln) for ln in text.splitlines() if ln.strip()]
    for ln in lines[:3]:
        if _looks_like_name(ln):
            c.name = ln
            break
    head = "\n".join(lines[:8])
    if m := _EMAIL.search(head) or _EMAIL.search(text):
        c.email = m.group(0)
    if m := _PHONE.search(head):
        c.phone = m.group(0).strip()
    contact_lines = [
        ln
        for ln in lines[:8]
        if _EMAIL.search(ln) or _PHONE.search(ln) or "http" in ln or "www." in ln
    ]
    for m in _URL.finditer("\n".join(contact_lines)):
        u = m.group(0)
        if c.email and (u in c.email or c.email.endswith(u)):
            continue
        kind = _url_kind(u)
        c.links.setdefault(kind, u if u.startswith("http") else f"https://{u}")
    return c


def redact(text: str, contact: Contact) -> str:
    """The resume without its contact lines or the candidate's name: what the model may read."""
    out = []
    for raw in text.splitlines():
        ln = raw
        if contact.email and contact.email in ln:
            continue
        if contact.phone and contact.phone in ln:
            continue
        if contact.name and _clean(ln) == contact.name:
            continue
        if contact.name:
            ln = ln.replace(contact.name, "[name]")
        out.append(ln)
    return "\n".join(out).strip() + "\n"


def _ym(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip().lower()
    if s in ("present", "current", "now"):
        return None
    if m := re.match(r"(\d{1,2})/(\d{4})", s):
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    if m := re.match(rf"({_MON})\s+(\d{{4}})", s):
        return f"{m.group(2)}-{_MONTHS.get(m.group(1)[:3], 1):02d}"
    if m := re.match(r"(\d{4})", s):
        return m.group(1)
    return None


def _split_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {"_top": []}
    current = "_top"
    for raw in text.splitlines():
        ln = _clean(raw)
        if not ln:
            continue
        key = re.sub(r"[^a-z ]", "", ln.lower()).strip()
        if key in _SECTIONS and len(ln) < 40:
            current = _SECTIONS[key]
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(raw.rstrip())
    return sections


def _entries(lines: list[str]) -> list[tuple[str, list[str]]]:
    """(header, bullets) blocks: a non-bullet line starts a new entry."""
    out: list[tuple[str, list[str]]] = []
    for raw in lines:
        if _BULLET.match(raw):
            if out:
                out[-1][1].append(_BULLET.sub("", raw).strip())
            continue
        ln = _clean(raw)
        if out and not out[-1][1] and not _RANGE.search(out[-1][0]) and _RANGE.search(ln):
            # a date line under its header ("Acme Corp" / "Engineer, Jun 2024 – present")
            out[-1] = (f"{out[-1][0]}, {ln}", [])
            continue
        out.append((ln, []))
    return out


def _split_head(head: str) -> tuple[str, str]:
    """'Org — Title' / 'Title, Org' / 'Title at Org' -> (left, right)."""
    for sep in (" — ", " – ", " | ", " - ", " at "):
        if sep in head:
            a, b = head.split(sep, 1)
            return a.strip(), b.strip()
    if ", " in head:
        a, b = head.split(", ", 1)
        return a.strip(), b.strip()
    return head.strip(), ""


def _experience(lines: list[str]) -> list[dict[str, Any]]:
    out = []
    for head, bullets in _entries(lines):
        rng = _RANGE.search(head)
        start = end = None
        if rng:
            start, end = _ym(rng.group("start")), _ym(rng.group("end"))
            head = (head[: rng.start()] + head[rng.end() :]).strip(" ,|–—-")
        org, title = _split_head(head)
        out.append(
            {
                "org": org or head,
                "title": title.strip(" ,") or "CHANGE_ME",
                "start": start,
                "end": end,
                "bullets": bullets,
            }
        )
    return out


def _education(lines: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    out: list[dict[str, Any]] = []
    coursework: list[str] = []
    for head, bullets in _entries(lines):
        low = head.lower()
        if low.startswith(("coursework", "relevant coursework")):
            coursework += [c.strip() for c in head.split(":", 1)[-1].split(",") if c.strip()]
            continue
        deg = _DEGREE.search(head)
        if not deg and out:
            out[-1].setdefault("notes", []).append(head)
            continue
        school, rest = _split_head(head)
        if deg and deg.start() < len(school) and rest:
            school, rest = rest, school  # "B.S. ..., School"
        grad = None
        if m := _SINGLE.search(rest or head):
            grad = _ym(m.group("d"))
        majors: list[str] = []
        minors: list[str] = []
        if deg:
            src = rest if deg.group(0) in rest else head
            after = src[src.index(deg.group(0)) + len(deg.group(0)) :]
            parts = [p.strip() for p in after.split(",")]
            major = re.sub(r"^(?:in|of)\s+", "", parts[0]).strip(" .")
            if major and not _SINGLE.fullmatch(major):
                majors = [major]
            for p in parts[1:]:
                if m := re.match(r"minors?\s+in\s+(.+)", p, re.I):
                    minors.append(m.group(1).strip())
        out.append(
            {
                "school": school,
                "degree": re.sub(r"\.", "", deg.group(1)) if deg else None,
                "majors": majors,
                "minors": minors,
                "graduation": grad,
                "coursework": [],
                "bullets": bullets,
            }
        )
    if out and coursework:
        out[0]["coursework"] = coursework
    for e in out:
        e.pop("notes", None)
        e.pop("bullets", None)
    return out, coursework


def _projects(lines: list[str]) -> list[dict[str, Any]]:
    out = []
    for head, bullets in _entries(lines):
        name, rest = _split_head(head)
        tech = [t.strip() for t in re.split(r",|/", rest) if t.strip()] if rest else []
        out.append(
            {
                "name": name,
                "summary": bullets[0] if bullets else "CHANGE_ME",
                "tech": tech,
                "bullets": bullets,
            }
        )
    return out


def _skills(lines: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for raw in lines:
        ln = _BULLET.sub("", _clean(raw))
        if ":" in ln:
            cat, items = ln.split(":", 1)
            key = re.sub(r"[^a-z0-9]+", "_", cat.lower()).strip("_") or "skills"
        else:
            key, items = "skills", ln
        vals = [s.strip() for s in re.split(r"[,;•·](?![^()]*\))", items) if s.strip()]
        out.setdefault(key, []).extend(vals)
    return out


def draft_profile(text: str, contact: Contact, resume_file: str) -> dict[str, Any]:
    """A profile.yaml draft. Values the code couldn't read are left out or set to CHANGE_ME, so
    an unreviewed draft can't pass validation."""
    s = _split_sections(text)
    education, _ = _education(s.get("education", []))
    if s.get("coursework") and education:
        education[0]["coursework"] += [
            c.strip() for ln in s["coursework"] for c in _clean(ln).split(",") if c.strip()
        ]
    summary = " ".join(_clean(x) for x in s.get("summary", [])).strip() or None
    links = [
        {"label": {"linkedin": "LinkedIn", "github": "GitHub"}.get(k, "Website"), "url": v}
        for k, v in contact.links.items()
    ]
    return {
        "version": 1,
        "headline": summary or "CHANGE_ME",
        "resumes": [{"file": resume_file, "default": True}],
        "education": education,
        "skills": _skills(s.get("skills", [])),
        "experience": _experience(s.get("experience", [])),
        "projects": _projects(s.get("projects", [])),
        "links": links,
    }
