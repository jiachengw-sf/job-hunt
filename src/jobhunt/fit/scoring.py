"""Stage 3: qualification band and preference score. All code, all
weights visible in config. Nothing here tunes itself."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

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
    labels: dict[str, str] = field(default_factory=dict)  # what matched, for the rank reason


_MODE_LABEL = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}


def _contains(term: str, text: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.I))


def preference(job: JobView, prefs: Preferences, cfg: FitConfig) -> Preference:
    comps: dict[str, float | None] = {}
    flags: list[str] = []
    labels: dict[str, str] = {"pay": "Pay"}
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
    if wm and modes & set(wm.value):
        labels["work_mode"] = next(
            _MODE_LABEL[m] for m in _MODE_LABEL if m in modes & set(wm.value)
        )
    # location
    places = prefs.location.places
    if places and places.value:
        if "remote" in modes and (wm is None or "remote" in wm.value):
            comps["location"] = 1.0
            labels["location"] = "Remote"
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
                    if best == 1.0 and "location" not in labels:
                        labels["location"] = pl.name.split(",")[0].strip()
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
        hit = next((v for v in tp.value if nz.norm_term(v) in set(terms)), None)
        if hit:
            labels["term"] = hit
    else:
        comps["term"] = None
    # keywords (Nice include/exclude; Must ones are gates)
    inc, exc = prefs.roles.include_keywords, prefs.roles.exclude_keywords
    kw: float | None = None
    if inc and not inc.must and inc.value:
        hit = next((k for k in inc.value if _contains(k, job.title)), None)
        kw = 1.0 if hit else 0.5
        if hit:
            labels["keywords"] = f'"{hit}"'
    if exc and not exc.must and exc.value:
        hit = next((k for k in exc.value if _contains(k, job.title)), None)
        if hit:
            kw = 0.0
            labels["keywords"] = f'"{hit}"'
    comps["keywords"] = kw
    # industry
    hay = f"{job.company}\n{job.title}\n{job.description}"
    pi, ai = prefs.company.prefer_industries, prefs.company.avoid_industries
    ind: float | None = None
    if (pi and pi.value) or (ai and ai.value and not ai.must):
        ind = 0.5
        good = next((t for t in pi.value if _contains(t, hay)), None) if pi else None
        if good:
            ind = 1.0
            labels["industry"] = good
        bad = next((t for t in ai.value if _contains(t, hay)), None) if ai and not ai.must else None
        if bad:
            ind = 0.0
            labels["industry"] = bad
    comps["industry"] = ind
    weights = cfg.preference.model_dump()
    used = {k: v for k, v in comps.items() if v is not None}
    if not used:
        return Preference(None, comps, flags, labels)
    total = sum(weights[k] for k in used)
    score = sum(weights[k] * v for k, v in used.items()) / total if total else None
    return Preference(round(score, 3) if score is not None else None, comps, flags, labels)
