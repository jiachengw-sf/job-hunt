"""Stage 3: qualification band, preference score, priority and the volume guard. All code, all
weights visible in config. Nothing here tunes itself."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from jobhunt.fit import normalize as nz
from jobhunt.fit.gates import GateResult
from jobhunt.fit.jobview import JobView
from jobhunt.fit.requirements import Requirement
from jobhunt.models import Preferences
from jobhunt.models.config import FitConfig

BANDS = ["not_a_fit", "stretch", "good", "strong"]
BAND_VALUE = {"strong": 1.0, "good": 0.75, "stretch": 0.4, "not_a_fit": 0.0}
UNKNOWN_BAND_VALUE = 0.3


def _cap(band: str, cap: str) -> str:
    return band if BANDS.index(band) <= BANDS.index(cap) else cap


@dataclass
class Qualification:
    band: str | None
    score: float | None
    provisional: bool
    rules: list[str] = field(default_factory=list)
    must: dict[str, int] = field(default_factory=dict)
    preferred: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _credit(r: Requirement, cfg: FitConfig) -> float:
    w = cfg.qualification
    if r.status == "met":
        return w.met_stated if r.tier == "stated" else w.met_inferred
    if r.status == "partial":
        return w.partial
    if r.status == "unmet":
        return w.unmet
    return w.unassessed


def _counts(reqs: list[Requirement]) -> dict[str, int]:
    out = {
        "total": len(reqs),
        "met_stated": 0,
        "met_inferred": 0,
        "partial": 0,
        "unmet": 0,
        "unassessed": 0,
    }
    for r in reqs:
        if r.status == "met":
            out["met_stated" if r.tier == "stated" else "met_inferred"] += 1
        elif r.status in ("partial", "unmet"):
            out[r.status] += 1
        else:
            out["unassessed"] += 1
    return out


def qualify(
    reqs: list[Requirement],
    gates: list[GateResult],
    cfg: FitConfig,
    *,
    required_years: float | None,
    your_years: float,
) -> Qualification:
    musts = [r for r in reqs if r.importance == "must"]
    prefs = [r for r in reqs if r.importance == "preferred"]
    unclear_gates = [g.gate for g in gates if g.result == "unclear"]
    if not reqs:
        rules = ["no requirements could be read from the posting; band needs the posting text"]
        return Qualification(None, None, True, rules, _counts(musts), _counts(prefs))
    m = sum(_credit(r, cfg) for r in musts) / len(musts) if musts else None
    p = sum(_credit(r, cfg) for r in prefs) / len(prefs) if prefs else None
    share = cfg.qualification.must_share
    if m is not None and p is not None:
        score = share * m + (1 - share) * p
    else:
        score = m if m is not None else p
    assert score is not None
    b = cfg.bands
    band = (
        "strong"
        if score >= b.strong
        else "good"
        if score >= b.good
        else "stretch"
        if score >= b.stretch
        else "not_a_fit"
    )
    rules: list[str] = []
    if any(r.status == "unmet" for r in musts):
        if BANDS.index(band) > BANDS.index("stretch"):
            rules.append("a must-have is unmet: at most stretch")
        band = _cap(band, "stretch")
    if band == "strong" and not all(r.status == "met" and r.tier == "stated" for r in musts):
        rules.append("strong needs every must-have met with a stated resume line: at most good")
        band = "good"
    if required_years is not None and required_years - your_years >= cfg.level_mismatch_years:
        if BANDS.index(band) > BANDS.index("stretch"):
            rules.append(
                f"level mismatch (asks {required_years:g}+ years, you have about {your_years:g}): "
                "at most stretch"
            )
        band = _cap(band, "stretch")
    provisional = any(r.status is None for r in reqs) or bool(unclear_gates)
    if unclear_gates:
        rules.append(f"unclear gates: {', '.join(unclear_gates)}")
    return Qualification(band, round(score, 3), provisional, rules, _counts(musts), _counts(prefs))


# --- preference -----------------------------------------------------------------------------


@dataclass
class Preference:
    score: float | None
    components: dict[str, float | None]
    flags: list[str]


def _contains(term: str, text: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.I))


def preference(job: JobView, prefs: Preferences, cfg: FitConfig) -> Preference:
    comps: dict[str, float | None] = {}
    flags: list[str] = []
    # pay
    lo, hi, note = nz.annual_pay(job.pay)
    if lo is None and hi is None:
        comps["pay"] = None
        flags.append("pay not listed")
    elif prefs.pay and prefs.pay.value.min is not None:
        floor = nz.to_annual(prefs.pay.value.min, prefs.pay.value.unit) or 0
        ideal = nz.to_annual(prefs.pay.value.ideal, prefs.pay.value.unit)
        top = hi if hi is not None else lo
        assert top is not None
        if top < floor:
            comps["pay"] = 0.0
        elif ideal and ideal > floor:
            comps["pay"] = round(min(1.0, 0.5 + 0.5 * (top - floor) / (ideal - floor)), 3)
        else:
            comps["pay"] = 1.0
        if note:
            flags.append(note)
    else:
        comps["pay"] = None
    # work mode
    modes = nz.work_modes(job.work_mode, job.locations, job.title, job.description)
    wm = prefs.location.work_modes
    comps["work_mode"] = (1.0 if modes & set(wm.value) else 0.0) if wm and modes else None
    # location
    places = prefs.location.places
    if places and places.value:
        if "remote" in modes and (wm is None or "remote" in wm.value):
            comps["location"] = 1.0
        else:
            best: float | None = None
            for loc in job.locations:
                for pl in places.value:
                    if nz.city_name(loc) == nz.city_name(pl.name):
                        best = 1.0
                    else:
                        d = nz.distance_miles(loc, pl.name)
                        if d is not None:
                            v = 1.0 if d <= (pl.radius_miles or 25) else 0.0
                            best = v if best is None else max(best, v)
            if best == 0.0 and prefs.location.relocation and prefs.location.relocation.value:
                best = 0.3
            comps["location"] = best
    else:
        comps["location"] = None
    # term
    tp = prefs.timing.terms
    terms = nz.job_terms(job.terms, job.title, job.description)
    if tp and tp.value and terms:
        wanted = {t for t in (nz.norm_term(v) for v in tp.value) if t}
        comps["term"] = 1.0 if wanted & set(terms) else 0.0
    else:
        comps["term"] = None
    # keywords (Nice include/exclude; Must ones are gates)
    inc, exc = prefs.roles.include_keywords, prefs.roles.exclude_keywords
    kw: float | None = None
    if inc and not inc.must and inc.value:
        kw = 1.0 if any(_contains(k, job.title) for k in inc.value) else 0.5
    if exc and not exc.must and exc.value and any(_contains(k, job.title) for k in exc.value):
        kw = 0.0
    comps["keywords"] = kw
    # industry
    hay = f"{job.company}\n{job.title}\n{job.description}"
    pi, ai = prefs.company.prefer_industries, prefs.company.avoid_industries
    ind: float | None = None
    if (pi and pi.value) or (ai and ai.value and not ai.must):
        ind = 0.5
        if pi and any(_contains(t, hay) for t in pi.value):
            ind = 1.0
        if ai and not ai.must and any(_contains(t, hay) for t in ai.value):
            ind = 0.0
    comps["industry"] = ind
    weights = cfg.preference.model_dump()
    used = {k: v for k, v in comps.items() if v is not None}
    if not used:
        return Preference(None, comps, flags)
    total = sum(weights[k] for k in used)
    score = sum(weights[k] * v for k, v in used.items()) / total if total else None
    return Preference(round(score, 3) if score is not None else None, comps, flags)


# --- priority -------------------------------------------------------------------------------


def _days_since(ts: str | None, today: date) -> float | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    except ValueError:
        return None
    return (today - d).days


@dataclass
class Priority:
    score: float
    components: dict[str, float]


def priority(
    job: JobView,
    band: str | None,
    pref_score: float | None,
    cfg: FitConfig,
    today: date,
    *,
    referral: bool,
    watchlist: bool,
    relevance: float = 0.0,
) -> tuple[Priority, list[str]]:
    flags: list[str] = []
    age = _days_since(job.posted_at, today)
    if age is None:
        age = _days_since(job.first_seen, today) or 0.0
    fresh = 0.5 ** (max(age, 0) / cfg.freshness_half_life_days)
    deadline = 0.0
    if job.deadline:
        left = -(_days_since(job.deadline, today) or 0)
        if left < 0:
            flags.append("deadline passed")
        else:
            deadline = max(0.0, 1 - left / 30)
    comps = {
        "band": BAND_VALUE.get(band or "", UNKNOWN_BAND_VALUE),
        "relevance": round(relevance, 3),
        "preference": pref_score if pref_score is not None else 0.5,
        "freshness": round(fresh, 3),
        "deadline": round(deadline, 3),
        "referral": 1.0 if referral else 0.0,
        "watchlist": 1.0 if watchlist else 0.0,
    }
    w = cfg.priority.model_dump()
    total = sum(w.values())
    score = sum(w[k] * comps[k] for k in comps) / total if total else 0.0
    return Priority(round(score, 3), comps), flags
