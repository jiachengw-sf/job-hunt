"""Resume text extraction and a first-draft profile.

Code extracts the text (PDF, DOCX, Markdown or plain text) and splits it into a draft
profile.yaml by section headers. The draft is starting material: the jobhunt-setup skill tidies
it from the *redacted* text (contact lines removed), and the user confirms it before it becomes
profile.yaml. Contact details found on the resume go only to the local form's pre-fill.

Some PDFs extract as one word per line. Such text is re-read in pypdf's layout mode and, if it
is still fragmented, rejoined into lines (at bullets, section headers and date ranges) before
sectioning. A section that still splits into an absurd number of entries is left out of the
draft with a warning, to be filled from the redacted text, rather than drafted wrong.
"""

from __future__ import annotations

import contextlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobhunt.errors import ValidationFailed

log = logging.getLogger(__name__)

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
_BULLET_GLYPH = frozenset("-•*▪●◦")
# inline "Category: items" lines that belong to skills or honors wherever they appear
_INLINE = {
    "languages": "skills",
    "programming languages": "skills",
    "frameworks": "skills",
    "libraries": "skills",
    "tools": "skills",
    "technologies": "skills",
    "databases": "skills",
    "platforms": "skills",
    "cloud": "skills",
    "certifications": "honors",
    "awards and honors": "honors",
}
# entries a section may plausibly hold; more means the text was split wrong
_MAX_ENTRIES = {"education": 8, "experience": 25, "projects": 25}


def fragmented(text: str) -> bool:
    """True when most lines hold a single word: the PDF came out one word per line."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 20:
        return False
    single = sum(1 for ln in lines if len(ln.split()) == 1)
    return single / len(lines) > 0.6


def _section_key(words: list[str]) -> str | None:
    key = re.sub(r"[^a-z ]", "", " ".join(words).lower()).strip()
    return _SECTIONS.get(key) and key


def rejoin(text: str) -> str:
    """Rebuild lines from one-word-per-line text: a bullet or a section header starts a line,
    and a line ends after a section header or a date range."""
    words = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out: list[str] = []
    cur: list[str] = []

    def flush() -> None:
        if not cur:
            return
        lo = 1 if cur[0] in _BULLET_GLYPH else 0
        line = " ".join(cur)
        if (_RANGE.search(line) or _SINGLE.search(line)) and (cut := _header_start(cur, lo)):
            out.append(" ".join(cur[:cut]))  # what came before the next entry's header
            del cur[:cut]
        out.append(" ".join(cur))
        cur.clear()

    i = 0
    while i < len(words):
        w = words[i]
        header = next(
            (
                n
                for n in (3, 2, 1)
                if all(x[:1].isupper() for x in words[i : i + n] if x.lower() != "and")
                and _section_key(words[i : i + n])
            ),
            0,
        )
        if header:
            flush()
            if words[i + header - 1].endswith(":"):  # an inline label: "Coursework: ..."
                cur.extend(words[i : i + header])
            else:
                out.append(" ".join(words[i : i + header]))
            i += header
            continue
        if w in _BULLET_GLYPH or _BULLET.match(w + " "):
            flush()
        cur.append(w)
        if (m := _RANGE.search(" ".join(cur))) and m.end() == len(" ".join(cur)):
            flush()
        i += 1
    flush()
    return "\n".join(out)


def _header_start(words: list[str], lo: int) -> int | None:
    """Where an entry header ("Org — Title, dates") begins in a line it was glued to (a bullet
    or a list before it): the run of capitalized words before its separator. None when the
    line starts with the header."""
    sep = next((k for k, w in enumerate(words) if w in ("—", "–", "|") and k > lo), None)
    if sep is None:
        return None
    k = sep
    while (
        k > lo
        and not words[k - 1].endswith((",", ";", ":", "."))
        and (words[k - 1][:1].isupper() or words[k - 1] in ("&", "of", "and", "the"))
    ):
        k -= 1
    return k if lo < k < sep else None


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if fragmented(text):
            layout = "\n".join(
                page.extract_text(extraction_mode="layout") or "" for page in reader.pages
            )
            layout = "\n".join(" ".join(ln.split()) for ln in layout.splitlines())
            if layout.strip() and not fragmented(layout):
                text = layout
    except Exception as e:
        raise ValidationFailed(f"{path.name}: couldn't read this PDF ({e}).") from e
    text = rejoin(text) if fragmented(text) else text
    if readability(text) < GOOD_ENOUGH:  # a scan, or a text layer that reads as noise
        from jobhunt.onboarding.ocr import ocr_pdf, unavailable

        if unavailable() is None:
            try:
                seen = ocr_pdf(path)
            except Exception as e:  # OCR is a fallback; the text layer stands
                log.warning("%s: OCR failed: %s", path.name, e)
            else:
                if readability(normalize_text(seen)) > readability(text):
                    return seen
    return text


GOOD_ENOUGH = 5


def readability(text: str) -> int:
    """How much of a resume a text shows: section headings found (3 each), an email (2), and
    up to 3 for length. A scan's empty text layer scores 0."""
    t = normalize_text(text or "")
    keys = {
        _SECTIONS[k]
        for ln in t.splitlines()
        if (k := re.sub(r"[^a-z ]", "", _clean(ln).lower()).strip()) in _SECTIONS and len(ln) < 40
    }
    words = len(t.split())
    return 3 * len(keys) + (2 if _EMAIL.search(t) else 0) + min(3, words // 100)


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValidationFailed(
            f"{path.name}: unsupported resume format; use PDF, DOCX, Markdown or plain text."
        )
    if suffix == ".pdf":
        text = _pdf_text(path)
    elif suffix == ".docx":
        import docx

        try:
            d = docx.Document(str(path))
        except Exception as e:
            raise ValidationFailed(f"{path.name}: couldn't read this DOCX ({e}).") from e
        text = "\n".join(_docx_lines(d))
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    text = normalize_text(text)
    if not text.strip():
        from jobhunt.onboarding.ocr import unavailable

        why = unavailable() if suffix == ".pdf" else None
        hint = f" To read scans, install OCR: {why}." if why else ""
        raise ValidationFailed(
            f"{path.name}: no text found (a scanned image?). Export a text PDF or DOCX "
            f"instead.{hint}"
        )
    return text


def _docx_lines(d: Any) -> list[str]:
    """A DOCX's text: the page header first (resumes often put the name and contact details
    there), then the body, tables, then the footer. A table cell holding several lines is a
    column of the layout, so each cell is its own block; a one-line row stays one line."""

    def para(p: Any) -> str:
        style = (p.style.name if p.style else "").lower()
        return f"- {p.text}" if "list" in style and p.text.strip() else p.text

    lines: list[str] = []
    for sec in d.sections[:1]:
        with contextlib.suppress(Exception):
            lines += [para(p) for p in sec.header.paragraphs]
    lines += [para(p) for p in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            cells = list(dict.fromkeys(row.cells))  # merged cells repeat
            if any(len([p for p in c.paragraphs if p.text.strip()]) > 2 for c in cells):
                for c in cells:
                    lines += [para(p) for p in c.paragraphs] + [""]
            else:
                lines.append(" | ".join(c.text for c in cells))
    for sec in d.sections[:1]:
        with contextlib.suppress(Exception):
            lines += [para(p) for p in sec.footer.paragraphs]
    return lines


_SPACED = re.compile(r"^(?:[A-Z&] ){2,}[A-Z&](?:\s{2,}(?:[A-Z&] ){1,}[A-Z&])*$")
_SMALLCAP = re.compile(r"\b([A-Z]) (?=[A-Z]{2,}\b)")


def normalize_text(text: str) -> str:
    """Ligatures to letters (ﬁ -> fi), odd spaces to spaces, and headings typeset letter by
    letter ("E D U C A T I O N", LaTeX small caps "A VERY  T ESTWELL") back to words. A
    LinkedIn or GitHub URL the layout wrapped onto the next line is joined."""
    text = unicodedata.normalize("NFKC", text.replace("\r", ""))
    out = []
    for ln in text.splitlines():
        st = ln.strip()
        if _SPACED.match(st):  # letter-spaced: words are separated by 2+ spaces
            ln = " ".join(w.replace(" ", "") for w in re.split(r"\s{2,}", st))
        elif st and st == st.upper() and _SMALLCAP.search(st):
            ln = re.sub(r"\s{2,}", " ", _SMALLCAP.sub(r"\1", st))
        out.append(ln)
    text = "\n".join(out)
    return re.sub(r"((?:linkedin|github)\.com/[\w/-]*-)\n(\w[\w-]*)", r"\1\2", text)


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
            c.name = ln.title() if ln.isupper() else ln  # "AVERY TESTWELL" (small caps)
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
        if (c.email and (u in c.email or c.email.endswith(u))) or "@" + u in text:
            continue
        kind = _url_kind(u)
        c.links.setdefault(kind, u if u.startswith("http") else f"https://{u}")
    # LinkedIn and GitHub profiles are unambiguous wherever they sit (a sidebar, a footer)
    for kind, rx in _PROFILE_URLS.items():
        if kind not in c.links and (m := rx.search(text)):
            u = m.group(0).rstrip(".,;)")
            c.links[kind] = u if u.startswith("http") else f"https://{u}"
    return c


_PROFILE_URLS = {
    "linkedin": re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w%-]+/?", re.I),
    "github": re.compile(r"(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9-]+(?![\w/.-]*@)", re.I),
}


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
    """Lines by section. An inline "Languages: ..." or "Awards: ..." line goes to skills or
    honors wherever it sits; "Technical Skills:" alone starts that section."""
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
        if m := re.match(r"^([A-Za-z][A-Za-z &/]{1,30}):\s*(\S.*)$", _BULLET.sub("", ln)):
            cat = re.sub(r"[^a-z ]", "", m.group(1).lower()).strip()
            dest = _INLINE.get(cat) or (_SECTIONS.get(cat) if cat in _SECTIONS else None)
            if dest in ("skills", "honors") and current != dest:
                sections.setdefault(dest, []).append(f"{m.group(1)}: {m.group(2)}")
                continue
        sections.setdefault(current, []).append(raw.rstrip())
    return sections


def _header_like(ln: str) -> bool:
    """An entry header: a date, an "Org — Title" separator, or a short Title Case line."""
    if _RANGE.search(ln) or _SINGLE.search(ln) or re.search(r" [—–|] | - ", ln):
        return True
    words = ln.split()
    return len(words) <= 6 and sum(w[:1].isupper() for w in words) >= max(1, len(words) - 1)


def _continues(bullet: str, raw: str, ln: str) -> bool:
    """A wrapped bullet's next line: indented, or starting lowercase, or after a bullet that
    hasn't ended, as long as it doesn't look like the next entry's header."""
    if not ln:
        return False
    if raw[:1].isspace() or ln[:1].islower() or ln[:1] in "(&,":
        return True
    return not bullet.rstrip().endswith((".", "!", "?", ":")) and not _header_like(ln)


def _sentence(ln: str) -> bool:
    """A bullet whose glyph the PDF lost: a sentence (6+ words, mostly lowercase after the
    first, no date range or "Org — Title" separator)."""
    words = ln.split()
    if len(words) < 6 or _RANGE.search(ln) or re.search(r" [—–|] ", ln):
        return False
    lower = sum(1 for w in words[1:] if w[:1].islower() or w[:1].isdigit())
    return lower >= (len(words) - 1) / 2


def _entries(lines: list[str]) -> list[tuple[str, list[str]]]:
    """(header, bullets) blocks: a non-bullet line starts a new entry, unless it carries on a
    bullet that wrapped, is a bullet whose glyph was lost, or is the second line of a two-line
    header ("Title  dates" over "Org  City, ST"; "Title — Org" over "dates")."""
    out: list[tuple[str, list[str]]] = []
    joined: set[int] = set()  # entries whose header already took a second line
    for raw in lines:
        if _BULLET.match(raw):
            if out:
                out[-1][1].append(_BULLET.sub("", raw).strip())
            continue
        ln = _clean(raw)
        if out and out[-1][1] and (
            _continues(out[-1][1][-1], raw, ln)
            or (len(ln.split()) == 1 and not out[-1][1][-1].rstrip().endswith((".", "!", "?")))
        ):  # fmt: skip
            out[-1][1][-1] = f"{out[-1][1][-1]} {ln}"
            continue
        if out and _sentence(ln):
            out[-1][1].append(ln)
            continue
        if out and not out[-1][1] and len(out) - 1 not in joined:
            prev_dates, dates = bool(_RANGE.search(out[-1][0])), bool(_RANGE.search(ln))
            if prev_dates != dates and len(ln.split()) <= 10 and ":" not in ln:
                out[-1] = (f"{out[-1][0]} | {ln}", [])
                joined.add(len(out) - 1)
                continue
        out.append((ln, []))
    return out


# A job title's head noun: it tells the title from the employer in "Title, Org" lines.
_ROLE = re.compile(
    r"\b(intern(?:ship)?|engineer|developer|analyst|scientist|assistant|associate|researcher|"
    r"manager|consultant|specialist|coordinator|lead|director|designer|technician|tutor|fellow|"
    r"officer|administrator|architect|representative|volunteer|president|founder|instructor|"
    r"teacher|programmer|co-?op|apprentice|trainee|head|chair|captain|editor|writer|advisor|"
    r"mentor|counselor|clerk|cashier|associate)s?\b",
    re.I,
)
_CITY_PREFIX = {
    "new", "san", "los", "las", "santa", "st", "st.", "saint", "fort", "palo", "salt", "ann",
    "mountain", "grand", "long", "baton", "little", "el", "des", "sioux", "cedar", "college",
    "menlo", "redwood", "oak", "overland", "park", "rock", "round", "silver", "sugar", "thousand",
    "virginia", "walnut", "white", "winston", "north", "south", "east", "west", "lake", "kansas",
    "oklahoma", "jersey", "iowa", "bowling", "coral", "daly", "half", "mount", "glen", "elk",
}  # fmt: skip
_PLACE = re.compile(r"(?:\s|^|,\s*)((?:(\S+)\s+)?(\S+)),\s*(?:[A-Z]{2}|Remote|USA|UK)\b")


def _drop_place(s: str) -> str:
    """Remove a trailing "City, ST" (one-word city, or two when the first is a city prefix
    like "New" or "San") and "Remote"."""

    def cut(m: re.Match[str]) -> str:
        first = m.group(2)
        if first and first.lower() in _CITY_PREFIX:
            return " "
        return f" {first} " if first else " "

    s = _PLACE.sub(cut, s)
    return re.sub(r"\bRemote\b", " ", s)


def title_org(head: str) -> tuple[str, str]:
    """(employer, title) from an experience header in any common order: "Org — Title",
    "Title, Org — City, ST (dates)", "Title at Org", "Title | dates | Org City, ST". The part
    naming a role (intern, engineer, assistant...) is the title; the first other part is the
    employer. Without a role word, the old order (employer first) stands."""
    h = _RANGE.sub(" ", head)
    h = re.sub(r"\(\s*\)", " ", h)
    h = _drop_place(h)
    parts = [p.strip(" ,()") for p in re.split(r"\s+[—–|-]\s+|\s+at\s+|,\s+|\s\|\s|\|", h)]
    parts = [p for p in parts if p and not re.fullmatch(r"(?:present|current|now)", p, re.I)]
    if not parts:
        return head.strip(), ""
    roles = [p for p in parts if _ROLE.search(p)]
    if len(roles) == 1 and len(parts) > 1:
        title = roles[0]
        org = next(p for p in parts if p is not title)
        return org, title
    org, title = _split_head(" — ".join(parts))
    return org, title


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
        org, title = title_org(head)
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


def _strip_tail(s: str) -> str:
    """A name without the dates, GPA or "City, ST" a layout put on the same line."""
    s = _RANGE.sub(" ", s)
    s = re.sub(r"\bGPA\b.*$", " ", s, flags=re.I)
    s = re.sub(r"\s+[A-Z][A-Za-z. ]+,\s*[A-Z]{2}\s*$", " ", s)
    s = re.sub(rf"\s+(?:{_MON}\s+)?(?:19|20)\d{{2}}\s*$", " ", s, flags=re.I)
    return " ".join(s.split()).strip(" ,|–—-")


def _merge_school_lines(entries: list[tuple[str, list[str]]]) -> list[tuple[str, list[str]]]:
    """Two-line education headers: "School  dates" over "Degree  GPA" (LaTeX templates), or
    "School" / "Degree" / "dates" in a sidebar: a line without a degree joins the next."""
    out: list[tuple[str, list[str]]] = []

    def school_like(h: str) -> bool:
        return ":" not in h and len(h.split()) <= 10 and not re.match(r"(relevant )?coursework",
                                                                      h, re.I)  # fmt: skip

    for head, bullets in entries:
        if out and not out[-1][1] and (
            (_DEGREE.search(head) and not _DEGREE.search(out[-1][0]) and school_like(out[-1][0]))
            or (not _DEGREE.search(head) and _DEGREE.search(out[-1][0])
                and (_RANGE.search(head) or _SINGLE.fullmatch(head.strip())
                     or re.match(r"^GPA\b", head, re.I)))
        ):  # fmt: skip
            out[-1] = (f"{out[-1][0]} — {head}", bullets)
            continue
        out.append((head, bullets))
    return out


def _education(lines: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    out: list[dict[str, Any]] = []
    coursework: list[str] = []
    for head, bullets in _merge_school_lines(_entries(lines)):
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
        if rng := _RANGE.search(head):  # "Sep 2024 – May 2028": graduation is the end
            grad = _ym(rng.group("end"))
            head_wo = (head[: rng.start()] + head[rng.end() :]).strip(" ,|–—-")
            school, rest = _split_head(head_wo)
            if deg and deg.start() < len(school) and rest:
                school, rest = rest, school
        elif dates := list(_SINGLE.finditer(rest or head)):
            grad = _ym(dates[-1].group("d"))
        school = _strip_tail(school)
        majors: list[str] = []
        minors: list[str] = []
        if deg:
            src = rest if deg.group(0) in rest else head
            after = src[src.index(deg.group(0)) + len(deg.group(0)) :]
            after = re.split(r"\s*(?:\(|\||—|–|;|\bGPA\b)", after, maxsplit=1, flags=re.I)[0]
            parts = [p.strip() for p in after.split(",")]
            major = re.sub(r"^[\s.]*(?:in|of)\s+", "", parts[0]).strip(" .")
            major = _strip_tail(major)
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


def _tech(rest: str) -> list[str]:
    """Tech names from a project header's tail, without its dates ("Streamlit2026" too)."""
    rest = re.sub(r"(?<=[A-Za-z+#])(?=(?:19|20)\d{2}\b)", " ", rest)
    rest = _RANGE.sub(" ", rest)
    rest = re.sub(rf"\b(?:{_MON}\s+)?(?:19|20)\d{{2}}\b|\b(?:present|current)\b", " ", rest,
                  flags=re.I)  # fmt: skip
    return [t.strip(" .") for t in re.split(r",|/|\||;", rest) if t.strip(" .–—-")]


def _projects(lines: list[str]) -> list[dict[str, Any]]:
    out = []
    for head, bullets in _entries(lines):
        name, rest = _split_head(head)
        tech = _tech(rest) if rest else []
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


def _honors(lines: list[str]) -> list[str]:
    out = []
    for raw in lines:
        ln = _BULLET.sub("", _clean(raw))
        items = ln.split(":", 1)[1] if re.match(r"^[A-Za-z &/]{1,30}:", ln) else ln
        out += [h.strip() for h in re.split(r";|,(?![^()]*\))", items) if h.strip()]
    return out


def draft_warnings(draft: dict[str, Any]) -> list[str]:
    """Why parts of a draft were left out (set by :func:`draft_profile`)."""
    return list(draft.get("_warnings") or [])


def draft_profile(text: str, contact: Contact, resume_file: str) -> dict[str, Any]:
    """A profile.yaml draft. Values the code couldn't read are left out or set to CHANGE_ME, so
    an unreviewed draft can't pass validation. ``_warnings`` (popped before writing) says which
    sections were left out because they split into an absurd number of entries."""
    s = _split_sections(text)
    education, _ = _education(s.get("education", []))
    if s.get("coursework") and education:
        education[0]["coursework"] += [
            c.strip() for ln in s["coursework"] for c in _clean(ln).split(",") if c.strip()
        ]
    if s.get("honors") and education:
        education[0]["honors"] = _honors(s["honors"])
    summary = " ".join(_clean(x) for x in s.get("summary", [])).strip() or None
    links = [
        {"label": {"linkedin": "LinkedIn", "github": "GitHub"}.get(k, "Website"), "url": v}
        for k, v in contact.links.items()
    ]
    out: dict[str, Any] = {
        "version": 1,
        "headline": summary or "CHANGE_ME",
        "resumes": [{"file": resume_file, "default": True}],
        "education": education,
        "skills": _skills(s.get("skills", [])),
        "experience": _experience(s.get("experience", [])),
        "projects": _projects(s.get("projects", [])),
        "links": links,
    }
    warnings = []
    for key, most in _MAX_ENTRIES.items():
        if len(out[key]) > most:
            warnings.append(
                f"{key}: the text split into {len(out[key])} entries, so it was left out of the "
                "draft; fill it from the redacted text"
            )
            out[key] = []
    if warnings:
        out["_warnings"] = warnings
    return out
