"""Text helpers for postings: HTML to text, title normalization, SimHash, timestamps."""

from __future__ import annotations

import hashlib
import html
import re
from datetime import UTC, datetime
from html.parser import HTMLParser

_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str | None) -> str | None:
    """Plain text from HTML (Greenhouse double-escapes its content, so unescape first)."""
    if not raw:
        return None
    if "&lt;" in raw:
        raw = html.unescape(raw)
    p = _TextExtractor()
    p.feed(raw)
    p.close()
    text = "".join(p.parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    out = "\n".join(ln for ln in lines if ln)
    return out or None


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", title.lower())).strip()


def content_hash(text: str | None) -> str | None:
    if not text:
        return None
    norm = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(norm.encode()).hexdigest()[:32]


def simhash(text: str | None, *, shingle: int = 3) -> str | None:
    """64-bit SimHash over word shingles, as 16 hex chars. Near-identical text -> small Hamming
    distance."""
    if not text:
        return None
    words = re.findall(r"[a-z0-9]+", text.lower())
    if not words:
        return None
    grams = (
        [" ".join(words[i : i + shingle]) for i in range(len(words) - shingle + 1)]
        if len(words) >= shingle
        else [" ".join(words)]
    )
    votes = [0] * 64
    for g in grams:
        h = int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            votes[bit] += 1 if (h >> bit) & 1 else -1
    value = sum(1 << bit for bit in range(64) if votes[bit] > 0)
    return f"{value:016x}"


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


# --- time -----------------------------------------------------------------------------------


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now() -> str:
    return iso(datetime.now(UTC))


def parse_ts(value: object) -> str | None:
    """A source timestamp (ISO string, epoch seconds or epoch milliseconds) as UTC ISO. Returns
    None for anything missing or unparseable: a posting date is never inferred."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        if isinstance(value, int | float) or (isinstance(value, str) and value.isdigit()):
            n = float(value)
            if n <= 0:
                return None
            if n > 1e11:  # milliseconds
                n /= 1000
            return iso(datetime.fromtimestamp(n, UTC))
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return iso(dt)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def days_between(earlier: str, later: str) -> float:
    a = datetime.fromisoformat(earlier.replace("Z", "+00:00"))
    b = datetime.fromisoformat(later.replace("Z", "+00:00"))
    return (b - a).total_seconds() / 86400
