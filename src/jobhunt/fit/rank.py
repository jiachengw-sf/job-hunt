"""The ranking: a fixed order of sort keys, not one blended score.

1. Relevance tier: strong, possible or weak match (see ``tier``).
2. Soft preferences: how many of the user's Nice preferences the job meets (more first).
3. Date: an upcoming application deadline (internships), then a known recent posting date
   (newest first), then an unknown date, then a known stale date.

The relevance score breaks what is still tied, then the job id. Each job carries a short reason
built from the same keys ("Strong match · Remote ✓ · Chicago ✓ · posted 2 days ago").
Gates are separate and unchanged: a gated-out job is never ranked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import cache

from jobhunt.config import read_yaml
from jobhunt.fit import normalize as nz
from jobhunt.fit.jobview import JobView
from jobhunt.fit.recall import description_slice, tokenize
from jobhunt.models import Preferences
from jobhunt.models.config import FitConfig
from jobhunt.paths import package_file

TIERS = ("strong", "possible", "weak")
TIER_LABEL = {"strong": "Strong match", "possible": "Possible match", "weak": "Weak match"}

# Short forms in titles, spelled out before matching a role family.
_ALIASES = {
    "swe": "software engineer",
    "sde": "software development engineer",
    "ml": "machine learning",
    "ai": "artificial intelligence",
    "bi": "business intelligence",
    "qa": "quality assurance",
    "sre": "site reliability engineer",
}
_WORD = re.compile(r"[a-z0-9+#]+")
_EVERGREEN = re.compile(
    r"\bevergreen\b|talent\s+(?:pipeline|community|pool|network)|general\s+(?:application|interest)"
    r"|future\s+opportunit|year[- ]round|throughout\s+the\s+year",
    re.I,
)


def _stems(text: str) -> list[str]:
    words = []
    for w in _WORD.findall(text.lower()):
        words += _ALIASES.get(w, w).split()
    return [w[:6] for w in words]


@cache
def family_phrases() -> dict[str, list[str]]:
    raw = read_yaml(package_file("defaults", "role_families.yaml")) or {}
    return {str(k).lower(): [str(p) for p in v] for k, v in raw.items()}


def role_match(title: str, prefs: Preferences) -> str | None:
    """The role family the title names, if any (``defaults/role_families.yaml``). Words compare
    on their first six letters: "engineering" matches "Engineer", "analytics" not "Analyst"."""
    fams = prefs.roles.families.value if prefs.roles.families else []
    have = set(_stems(title))
    for fam in fams:
        for phrase in family_phrases().get(fam.lower().strip(), [fam]):
            need = _stems(phrase)
            if need and all(w in have for w in need):
                return fam
    return None


def has_text(job: JobView, min_words: int) -> bool:
    return len(description_slice(job.description, min_words)) >= min_words


def evergreen(job: JobView) -> bool:
    return bool(_EVERGREEN.search(job.title) or _EVERGREEN.search(job.description[:3000]))


# --- keys -----------------------------------------------------------------------------------


def tier(
    relevance: float, role: str | None, text: bool, cfg: FitConfig, *, related: bool = True
) -> tuple[str, str | None]:
    """(tier, note). Strong needs a role match, the posting text and relevance at or above
    ``fit.tiers.strong``. A title-only row is at most possible. A role match, or relevance at or
    above ``fit.tiers.possible`` with a title that shares a word with your search (``related``),
    is possible: boilerplate text alone can't lift an unrelated title. The rest is weak."""
    t = cfg.tiers
    if role and text and relevance >= t.strong:
        return "strong", None
    if role and not text:
        return "possible", "title only"
    if role or (related and relevance >= t.possible):
        return "possible", None
    return "weak", None


def title_related(title: str, query_terms: frozenset[str]) -> bool:
    """Whether the title shares a search word with your resume, role families or keywords."""
    return bool(set(tokenize(title)) & query_terms)


def demote(t: str) -> str:
    return TIERS[min(TIERS.index(t) + 1, len(TIERS) - 1)]


@dataclass
class Soft:
    count: int
    met: list[str] = field(default_factory=list)  # "Remote ✓"
    missed: list[str] = field(default_factory=list)  # "senior ✗"


def soft_matches(
    comps: dict[str, float | None],
    labels: dict[str, str],
    *,
    referral: bool,
    watchlist: bool,
) -> Soft:
    """One point per Nice preference the job meets; one off for a Nice exclude keyword or an
    industry to avoid that it hits. Unknown values count neither way."""
    s = Soft(0)

    def met(key: str) -> None:
        s.count += 1
        s.met.append(f"{labels.get(key, key)} ✓")

    if comps.get("work_mode") == 1.0:
        met("work_mode")
    # a remote job meets "location" through remote; count remote once
    if comps.get("location") == 1.0 and labels.get("location") != labels.get("work_mode"):
        met("location")
    if (comps.get("pay") or 0) >= 0.5:
        met("pay")
    if comps.get("term") == 1.0:
        met("term")
    for key in ("keywords", "industry"):
        v = comps.get(key)
        if v == 1.0:
            met(key)
        elif v == 0.0 and key in labels:
            s.count -= 1
            s.missed.append(f"{labels[key]} ✗")
    if watchlist:
        s.count += 1
        s.met.append("watchlist ✓")
    if referral:
        s.count += 1
        s.met.append("referral ✓")
    return s


def _day(ts: str | None) -> date | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def is_internship(job: JobView) -> bool:
    return (
        nz.employment_type(job.employment_type, job.title) in ("internship", "co-op")
        or nz.seniority(job.title) == "internship"
    )


def _ago(days: int) -> str:
    if days <= 0:
        return "posted today"
    if days == 1:
        return "posted yesterday"
    if days < 60:
        return f"posted {days} days ago"
    return f"posted {days // 30} months ago"


def date_key(job: JobView, today: date, cfg: FitConfig) -> tuple[int, int, str]:
    """(bucket, order within it, text). Buckets: 0 upcoming deadline (internships), 1 known
    recent date, 2 unknown (no date, or an evergreen post), 3 known stale date or passed
    deadline."""
    due = _day(job.deadline)
    if due is not None and due < today:
        return 3, 10**6, f"deadline passed {due:%b} {due.day}"
    if due is not None and is_internship(job):
        return 0, (due - today).days, f"apply by {due:%b} {due.day}"
    if evergreen(job):
        return 2, 0, "always open"
    posted = _day(job.posted_at)
    if posted is None:
        return 2, 0, "date unknown"
    age = max((today - posted).days, 0)
    if age <= cfg.stale_after_days:
        return 1, age, _ago(age)
    return 3, age, _ago(age)


# --- all together ---------------------------------------------------------------------------


@dataclass
class RankInput:
    job: JobView
    relevance: float
    role: str | None
    text: bool
    soft: Soft
    band: str | None = None
    related: bool = True  # the title shares a search word (see ``title_related``)
    degree_above: str | None = None
    form_filter: str | None = None  # "skip" | "deprioritize"


@dataclass
class Ranked:
    tier: str
    soft: int
    reason: str
    rank: int = 0
    key: tuple[object, ...] = ()


def rank(items: list[RankInput], today: date, cfg: FitConfig) -> dict[int, Ranked]:
    out: dict[int, Ranked] = {}
    for it in items:
        t, note = tier(it.relevance, it.role, it.text, cfg, related=it.related)
        notes = [note] if note else []
        if it.band == "not_a_fit" and t != "weak":
            t = "weak"
            notes.append("not a fit")
        if it.degree_above or it.form_filter == "deprioritize":
            t = demote(t)
            notes.append(
                f"lists {it.degree_above}" if it.degree_above else "form asks for what you lack"
            )
        skip = it.form_filter == "skip"
        if skip:
            t = "weak"
            notes.append("your form filter skips it")
        bucket, order, when = date_key(it.job, today, cfg)
        head = TIER_LABEL[t] + (f" ({', '.join(notes)})" if notes else "")
        parts = [head, *it.soft.met, *it.soft.missed, when]
        key = (
            int(skip),
            TIERS.index(t),
            -it.soft.count,
            bucket,
            order,
            -round(it.relevance, 4),
            it.job.id,
        )
        out[it.job.id] = Ranked(t, it.soft.count, " · ".join(p for p in parts if p), key=key)
    for i, jid in enumerate(sorted(out, key=lambda j: out[j].key), start=1):
        out[jid].rank = i
    return out


# --- for queries ----------------------------------------------------------------------------

# ORDER BY fragment for the `fits` table aliased `f`: ranked jobs first, in rank order.
RANK_ORDER = "f.rank IS NULL, f.rank"


def tiers_from(best: str) -> list[str]:
    """``best`` and every tier above it: tiers_from("possible") == ["strong", "possible"]."""
    return list(TIERS[: TIERS.index(best) + 1])


def tier_clause(best: str) -> tuple[str, list[str]]:
    """SQL condition (on ``f``) for jobs in tier ``best`` or better, with its parameters."""
    ts = tiers_from(best)
    return f"f.tier IN ({','.join('?' * len(ts))})", ts


def hide_weak_clause(cfg: FitConfig) -> str:
    """SQL condition (on ``f``) that leaves weak matches out when ``fit.hide_weak`` is on."""
    return "coalesce(f.tier, '') != 'weak'" if cfg.hide_weak else "1=1"
