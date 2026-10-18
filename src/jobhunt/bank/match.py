"""Matching a form label to a canonical question, and a chosen value to a select's options.

Order: the ATS's standard field name; a wording the user confirmed; a curated wording from
``defaults/question_synonyms.yaml``; the question text of the user's own bank entries. All of
those are confident. Anything else that is merely *similar* (token and character overlap, or
embeddings when available) is a ``candidate``: the answer is reused only after the user confirms
the new wording once. Below the threshold the label is ``unknown``. Nothing is ever guessed.
"""

from __future__ import annotations

import html
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import cache
from typing import Literal

from jobhunt.bank.keys import BUILTIN
from jobhunt.config import read_yaml
from jobhunt.models import Answers
from jobhunt.paths import package_file

MatchStatus = Literal["exact", "confirmed", "candidate", "unknown"]
CANDIDATE_THRESHOLD = 0.6

_FILLER = re.compile(
    r"\b(required|optional|please|kindly|select all that apply|check all that apply|select one|"
    r"if applicable)\b"
)
_STOP = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "to",
        "in",
        "for",
        "on",
        "at",
        "by",
        "or",
        "and",
        "is",
        "are",
        "you",
        "your",
        "do",
        "did",
        "does",
        "be",
        "will",
        "would",
        "with",
        "this",
        "that",
        "any",
        "have",
        "has",
        "from",
        "as",
        "it",
        "its",
        "we",
        "our",
        "us",
        "please",
    }
)


def _plain(s: str) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or "")).lower()
    s = re.sub(r"['’]s\b", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def company_template(label: str, company: str | None) -> str:
    """The label with the company's name as "[Company]", so a confirmed wording carries over to
    other companies' forms."""
    if not company or not company.strip():
        return label
    return re.sub(rf"(?<!\w){re.escape(company.strip())}(?!\w)", "[Company]", label, flags=re.I)


def normalize(label: str, company: str | None = None) -> str:
    """Lowercase, no markup or punctuation, filler removed, the job's company named "company"."""
    s = f" {_plain(label)} "
    if company and (name := _plain(company)):
        s = s.replace(f" {name} ", " company ")
    s = _FILLER.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _stem(t: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[: -len(suf)]
    return t


def content_tokens(norm: str) -> list[str]:
    return [_stem(t) for t in norm.split() if t not in _STOP]


def _trigrams(s: str) -> Counter[str]:
    s = f"  {s} "
    return Counter(s[i : i + 3] for i in range(len(s) - 2))


def similarity(a: str, b: str) -> float:
    """0..1 between two normalized labels: mean of content-token Dice and trigram cosine."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(content_tokens(a)), set(content_tokens(b))
    dice = 2 * len(ta & tb) / (len(ta) + len(tb)) if ta and tb else 0.0
    ca, cb = _trigrams(a), _trigrams(b)
    dot = sum(ca[k] * cb[k] for k in ca.keys() & cb.keys())
    cos = dot / (
        math.sqrt(sum(v * v for v in ca.values())) * math.sqrt(sum(v * v for v in cb.values()))
    )
    return (dice + cos) / 2


@cache
def seed_wordings() -> dict[str, list[str]]:
    """key -> normalized curated wordings."""
    raw = read_yaml(package_file("defaults", "question_synonyms.yaml")) or {}
    return {k: sorted({normalize(str(w)) for w in v or []}) for k, v in raw.items()}


@dataclass
class Match:
    key: str | None
    status: MatchStatus
    score: float = 0.0
    via: str = ""  # field_name | confirmed | synonyms | bank_question | similarity
    wording: str | None = None  # the known wording it matched

    @property
    def confident(self) -> bool:
        return self.status in ("exact", "confirmed")


@dataclass
class Matcher:
    """Built once per run from answers.yaml (confirmed wordings and custom entries)."""

    answers: Answers
    embed: Callable[[list[str]], list[list[float]]] | None = None
    threshold: float = CANDIDATE_THRESHOLD
    _confirmed: dict[str, str] = field(default_factory=dict)  # normalized wording -> key
    _questions: dict[str, str] = field(default_factory=dict)  # normalized custom question -> key
    _known: list[tuple[str, str]] = field(default_factory=list)  # (normalized wording, key)

    def __post_init__(self) -> None:
        for e in self.answers.bank:
            for w in e.wordings:
                self._confirmed[normalize(w)] = e.key
            if e.key not in BUILTIN:
                self._questions[normalize(e.question)] = e.key
        seeds = seed_wordings()
        known: dict[str, str] = {}
        for k, ws in seeds.items():
            for w in ws:
                known.setdefault(w, k)
        known.update(self._questions)
        known.update(self._confirmed)
        self._known = sorted(known.items())

    def match(self, label: str, *, field_name: str | None = None, company: str = "") -> Match:
        if field_name:
            from jobhunt.bank.keys import FIELD_NAMES

            k = FIELD_NAMES.get(field_name.lower())
            if k:
                return Match(k, "exact", 1.0, "field_name")
        norm = normalize(label, company)
        if not norm:
            return Match(None, "unknown")
        for n in (norm, normalize(label)):  # confirmed with "[Company]", or verbatim
            if n in self._confirmed:
                return Match(self._confirmed[n], "confirmed", 1.0, "confirmed", n)
        for k, ws in seed_wordings().items():
            if norm in ws:
                return Match(k, "exact", 1.0, "synonyms", norm)
        if norm in self._questions:
            return Match(self._questions[norm], "confirmed", 1.0, "bank_question", norm)
        best = self._best_similar(norm)
        if best and best[0] >= self.threshold:
            return Match(best[2], "candidate", round(best[0], 3), "similarity", best[1])
        return Match(None, "unknown", round(best[0], 3) if best else 0.0)

    def _best_similar(self, norm: str) -> tuple[float, str, str] | None:
        if not self._known:
            return None
        scored = [(similarity(norm, w), w, k) for w, k in self._known]
        if self.embed is not None:
            vecs = self.embed([norm, *(w for w, _ in self._known)])
            q = vecs[0]
            for i, (s, w, k) in enumerate(scored):
                scored[i] = (max(s, _cos(q, vecs[i + 1])), w, k)
        return max(scored)


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


# --- select options --------------------------------------------------------------------------


def choose_option(
    candidates: Iterable[str], options: list[str], *, yesno: bool = False
) -> str | None:
    """The form option equal to one of the acceptable texts (in preference order), else None.

    Yes/no answers also accept an option whose first word is that answer ("Yes, I am
    authorized"), but only when exactly one option starts that way.
    """
    norm_opts = [(normalize(o), o) for o in options]
    cands = [c for c in candidates if c]
    for c in cands:
        nc = normalize(c)
        for no, o in norm_opts:
            if no == nc:
                return o
    if yesno:
        for c in cands:
            first = normalize(c).split(" ")[0] if normalize(c) else ""
            if first not in ("yes", "no", "true", "false"):
                continue
            hits = [o for no, o in norm_opts if no.split(" ")[0:1] == [first]]
            if len(hits) == 1:
                return hits[0]
    return None
