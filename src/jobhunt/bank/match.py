"""Matching a form label to a canonical question, and a chosen value to a select's options.

Order: the ATS's standard field name; a wording the user confirmed; a curated wording from
``defaults/question_synonyms.yaml``; the question text of the user's own bank entries. All of
those are confident. Anything else that is merely *similar* (token and character overlap, or
embeddings when available) is a ``candidate``. With ``fill_similar`` (config
``forms.fill_similar``) a clear candidate is filled from its key's answer and flagged for the
user's review before Submit, and submitting teaches the bank that wording; otherwise the user
confirms it once first. Below the threshold the label is ``unknown`` and goes to the user.

Location questions name places ("relocate to Austin, TX", "onsite in our Seattle office"); the
place is replaced by "place" before matching, so one confirmed wording covers every city.
Similarity runs on a canonical paraphrase ("lawfully" = "legally", "eligible to work" =
"authorized to work", "visa sponsorship" = "sponsor"), and only between labels that ask about the
same things: "authorized to work without sponsorship" asks two things, so it never becomes a
candidate for the authorization question alone.
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


# a location question: its place names are placeholders, not part of the question
_LOCATION_CUE = re.compile(
    r"\b(relocat\w*|on-?site|in-?person|in the office|office|commut\w*|reside|located|live in|"
    r"work from|hybrid|move to)\b",
    re.I,
)
_PLACE = re.compile(
    r"\b(?:to|in|at|near|from|around|or|and)\s+(?:the\s+|our\s+)?"
    r"((?:[A-Z][\w.'-]*(?:\s+(?:[A-Z][\w.'-]*|of\b|de\b|la\b))*)(?:,\s*[A-Z]{2}\b)?)"
)
_PLACE_START = re.compile(r"^((?:[A-Z][\w.'-]*\s+){0,2}[A-Z][\w.'-]*),\s*[A-Z]{2}\b")


def placeless(label: str) -> str:
    """A location question with its places as "place": "relocate to Austin, TX?" and "relocate
    to New York City?" read the same. Other questions are left alone."""
    if not _LOCATION_CUE.search(label or ""):
        return label
    out = _PLACE.sub(lambda m: m.group(0)[: m.start(1) - m.start(0)] + "place", label)
    return _PLACE_START.sub("place", out)


_CANON = [
    (re.compile(r"\blawfully\b"), "legally"),
    (re.compile(r"\b(?:eligible|permitted|allowed|able) to (?:work|be employed)\b"),
     "authorized to work"),
    (re.compile(r"\b(?:visa |immigration |employer |employment )?sponsor(?:ship|ed|ing)?\b"),
     "sponsor"),
    (re.compile(r"\b(?:u s a?|usa|united states(?: of america)?|this country|the country(?: "
                r"where this job is located| in which you are applying| for which you are "
                r"applying)?)\b"), "country"),
    (re.compile(r"\b(?:on site|onsite|in person|in the office|from the office)\b"), "onsite"),
    (re.compile(r"\b(?:relocation|relocating)\b"), "relocate"),
    (re.compile(r"\b(?:five|5) days (?:a|per) week\b|\bfull time onsite\b"), "5 days"),
    (re.compile(r"\b(?:need|require)\b"), "require"),
]  # fmt: skip
# what a question asks about; a candidate must ask about the same things
_CONCEPTS = {
    "authorized": re.compile(r"\bauthori[sz]ed? to work\b|\bwork authori[sz]ation\b"),
    "sponsor": re.compile(r"\bsponsor\b"),
    "citizen": re.compile(r"\bcitizen"),
    "relocate": re.compile(r"\breloca"),
    "onsite": re.compile(r"\bonsite\b|\b5 days\b"),
    "remote": re.compile(r"\bremote\b"),
    "high_school": re.compile(r"\bhigh school\b|\bsecondary school\b"),
    # applied before vs. worked there before vs. relatives there: three different answers
    "applied_before": re.compile(
        r"\b(?:previously|ever|before|already|prior) (?:been )?appl|\bappl\w* (?:before|"
        r"previously|in the past)\b|\bprior application"
    ),
    "worked_before": re.compile(
        r"\b(?:previously|ever|before|formerly|prior) (?:been )?(?:employ|work)|\bworked (?:at|"
        r"for)\b|\bformer (?:employee|intern|worker)\b|\bprevious worker\b"
    ),
    "relatives": re.compile(r"\brelative|\bfamily member"),
    "age": re.compile(r"\bage\b|\byears old\b|\bolder\b|\byounger\b"),
}
_TIME = {"now": re.compile(r"\bnow\b|\bcurrently\b|\bat this time\b"),
         "future": re.compile(r"\bfuture\b")}  # fmt: skip


def canonical(norm: str) -> str:
    """A normalized label in canonical words, for similarity only."""
    for rx, rep in _CANON:
        norm = rx.sub(rep, norm)
    return re.sub(r"\s+", " ", norm).strip()


def concepts(canon: str) -> tuple[frozenset[str], frozenset[str]]:
    """(what it asks about, when: now / future) for a canonical label."""
    what = frozenset(k for k, rx in _CONCEPTS.items() if rx.search(canon))
    when = frozenset(k for k, rx in _TIME.items() if rx.search(canon))
    return what, when


# Words that turn a question around. "Are you NOT authorized", "unable to", "without
# sponsorship": similar characters, opposite answer.
_NEG_WORDS = frozenset({"not", "never", "cannot", "without", "nor", "neither"})
_NEG_CONTRACTIONS = frozenset(
    {"can", "don", "doesn", "didn", "won", "wouldn", "isn", "aren", "wasn", "weren", "haven",
     "hasn", "hadn", "shouldn", "couldn", "mustn"}
)  # fmt: skip
_NEG_PREFIXES = ("un", "in", "non", "dis", "im", "il", "ir")
# positive words whose prefixed form ("unwilling", "ineligible") reverses the question
_POSITIVE = frozenset(
    {"able", "willing", "authorized", "authorised", "eligible", "lawful", "lawfully", "legal",
     "legally", "available", "capable", "employed", "permitted", "allowed", "qualified",
     "interested", "comfortable", "possible", "certified", "licensed", "registered", "regular",
     "active", "valid"}
)  # fmt: skip
# comparisons that a number in the question hangs on: "under 18" and "at least 18" differ
_BELOW = re.compile(
    r"\b(?:under|below|less than|fewer than|younger than|at most|no more than|maximum of|"
    r"not yet|up to)\b"
)
_ABOVE = re.compile(
    r"\b(?:at least|over|above|more than|older than|or older|or above|or more|or over|"
    r"minimum|plus)\b"
)
_NUMBER_WORDS = {
    "thirteen": "13", "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty one": "21", "twenty five": "25",
}  # fmt: skip


def negations(norm: str) -> int:
    """How many times a normalized label turns itself around."""
    toks = norm.split()

    def turned(i: int, t: str) -> bool:
        prev = toks[i - 1] if i else ""
        return (
            t in _NEG_WORDS
            or (t == "t" and prev in _NEG_CONTRACTIONS)  # "can't" -> "can t"
            or (t == "longer" and prev == "no")
            or any(t.startswith(p) and t[len(p) :] in _POSITIVE for p in _NEG_PREFIXES)
        )

    n = sum(turned(i, t) for i, t in enumerate(toks))
    return n


def numbers(norm: str) -> frozenset[str]:
    for w, d in _NUMBER_WORDS.items():
        norm = re.sub(rf"\b{w}\b", d, norm)
    return frozenset(str(int(x)) for x in re.findall(r"\b\d+\b", norm))


def comparisons(norm: str) -> frozenset[str]:
    """Which way a number in the question points: below, above (only when it has a number)."""
    if not numbers(norm):
        return frozenset()
    return frozenset(k for k, rx in (("below", _BELOW), ("above", _ABOVE)) if rx.search(norm))


def same_question(a: str, b: str) -> bool:
    """True unless the two canonical labels ask different things: different topics, a different
    time (a time word on one side only is allowed: "do you require sponsorship" may mean now),
    opposite polarity ("not", "unable", "without"), or different numbers or comparisons
    ("under 18" vs "at least 18" vs "at least 21"; a limit on one side only)."""
    (wa, ta), (wb, tb) = concepts(a), concepts(b)
    if wa != wb or (ta and tb and ta != tb):
        return False
    if negations(a) % 2 != negations(b) % 2:
        return False
    na, nb = numbers(a), numbers(b)
    if na and nb:
        return na == nb and comparisons(a) == comparisons(b)
    # a number on one side only ("the full 10-12 week summer" vs "all summer") is detail, unless
    # it's a limit ("at least 3 years", "under 18"): then it's a different question
    return not comparisons(a) and not comparisons(b)


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
    alt_key: str | None = None  # similarity only: the best *other* key, if any
    alt_score: float = 0.0

    @property
    def confident(self) -> bool:
        return self.status in ("exact", "confirmed")


@dataclass
class Matcher:
    """Built once per run from answers.yaml (confirmed wordings and custom entries)."""

    answers: Answers
    embed: Callable[[list[str]], list[list[float]]] | None = None
    threshold: float = CANDIDATE_THRESHOLD
    # Fill a similar wording from its key's answer (flagged for the user's review) instead of
    # asking first; see ``forms/classify.py``. Config ``forms.fill_similar``.
    fill_similar: bool = False
    _confirmed: dict[str, str] = field(default_factory=dict)  # normalized wording -> key
    _proposed: dict[str, str] = field(default_factory=dict)  # agent-mapped wording -> key
    _questions: dict[str, str] = field(default_factory=dict)  # normalized custom question -> key
    _known: list[tuple[str, str]] = field(default_factory=list)  # (normalized wording, key)

    def __post_init__(self) -> None:
        for e in self.answers.bank:
            for w in e.wordings:
                self._confirmed[normalize(placeless(w))] = e.key
            for w in e.proposed:
                self._proposed[normalize(placeless(w))] = e.key
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
        from jobhunt.bank.keys import AGE_CHECK, age_question

        if age_question(label):  # "at least 21?", "under 18?": answered from the user's age
            return Match(AGE_CHECK, "exact", 1.0, "age")
        label = placeless(label)
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
        for n in (norm, normalize(label)):  # mapped by the agent: filled for the user's review
            if n in self._proposed:
                return Match(self._proposed[n], "candidate", 1.0, "agent", n)
        ranked = self._ranked_similar(norm)
        best = ranked[0] if ranked else None
        if best and best[0] >= self.threshold:
            alt = next((r for r in ranked if r[2] != best[2]), None)
            return Match(
                best[2], "candidate", round(best[0], 3), "similarity", best[1],
                alt_key=alt[2] if alt else None, alt_score=round(alt[0], 3) if alt else 0.0,
            )  # fmt: skip
        return Match(None, "unknown", round(best[0], 3) if best else 0.0)

    def _best_similar(self, norm: str) -> tuple[float, str, str] | None:
        """The most similar known wording that asks about the same things."""
        ranked = self._ranked_similar(norm)
        return ranked[0] if ranked else None

    def _ranked_similar(self, norm: str) -> list[tuple[float, str, str]]:
        """Known wordings that ask about the same things, most similar first."""
        known = [(w, k) for w, k in self._known if same_question(canonical(norm), _canon(w))]
        if not known:
            return []
        q = canonical(norm)
        scored = [(max(similarity(norm, w), similarity(q, _canon(w))), w, k) for w, k in known]
        if self.embed is not None:
            vecs = self.embed([norm, *(w for w, _ in known)])
            for i, (s, w, k) in enumerate(scored):
                scored[i] = (max(s, _cos(vecs[0], vecs[i + 1])), w, k)
        return sorted(scored, reverse=True)


@cache
def _canon(norm: str) -> str:
    return canonical(norm)


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
