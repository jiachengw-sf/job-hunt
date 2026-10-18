"""Quote and citation checks shared by every result kind.

Matching is exact after normalization only: Unicode NFKC, hidden characters removed, curly
quotes and dashes unified, case folded, whitespace collapsed. Nothing fuzzy.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from jobhunt.fit.profile_lines import ProfileLine

# Zero-width, bidi controls, soft hyphen, word joiners, BOM, and the Unicode "tag" block that can
# carry invisible text.
HIDDEN = re.compile("[­᠎​-‏‪-‮⁠-⁤⁦-⁯﻿￹-￻\U000e0000-\U000e007f]")
_QUOTES = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "′": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "″": '"',
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "−": "-",
        "•": " ",
        "·": " ",
    }
)
MIN_QUOTE_CHARS = 3


def strip_hidden(text: str) -> str:
    """Remove invisible characters (zero-width, bidi controls, tag characters) from text."""
    return HIDDEN.sub("", text)


def clean_text(text: str) -> str:
    """Generated text as stored: NFC, no hidden characters, no control chars but newlines/tabs."""
    text = strip_hidden(unicodedata.normalize("NFC", text))
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) != "Cc")


def norm(text: str) -> str:
    s = strip_hidden(unicodedata.normalize("NFKC", text)).translate(_QUOTES).casefold()
    return re.sub(r"\s+", " ", s).strip()


def _trim(q: str) -> str:
    return q.strip(" .,;:!?\"'()[]-*")


def quote_in(quote: str, text: str) -> bool:
    """True when ``quote`` appears verbatim (after normalization) in ``text``."""
    q = _trim(norm(quote))
    return len(q) >= MIN_QUOTE_CHARS and q in norm(text)


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


@dataclass
class Problem:
    """One reason a result was rejected: where (a JSON path) and what to fix."""

    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


def check_line(
    ref: str | None,
    quote: str | None,
    lines: dict[str, ProfileLine],
    path: str,
    problems: list[Problem],
) -> ProfileLine | None:
    """A citation of the user's profile: ``ref`` must name a line and ``quote`` must be that line's
    text (or a verbatim part of it). Returns the line on success."""
    if not ref:
        problems.append(Problem(f"{path}.evidence_ref", "missing: cite a profile line ref"))
        return None
    line = lines.get(ref)
    if line is None:
        problems.append(
            Problem(f"{path}.evidence_ref", f"{ref!r} is not a profile line ref in the task")
        )
        return None
    if not quote:
        problems.append(Problem(f"{path}.evidence_quote", "missing: quote the profile line"))
        return None
    if not quote_in(quote, line.text):
        problems.append(
            Problem(
                f"{path}.evidence_quote",
                f"not found in profile line {ref}; quote its text exactly: {line.text!r}",
            )
        )
        return None
    return line
