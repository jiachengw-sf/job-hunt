"""Stage 0: gates. Drop postings that fail a Must preference or a knockout requirement.

Each gate returns one of:
  pass             the posting meets it, with the matched text as evidence
  pass_unverified  the posting says nothing about it (kept)
  unclear          ambiguous: kept, and routed to the user (sensitive facts) or a judgment task
  fail             the posting fails it; the job is gated out, and the reason is always shown

Gates read sensitive answers (work authorization, citizenship, clearance) in code only. Those
answers never reach the model; an unclear sensitive gate becomes a question for the user.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Literal

from jobhunt.fit import normalize as nz
from jobhunt.fit.jobview import JobView
from jobhunt.fit.profile_lines import (
    DEGREE_LEVELS,
    current_degrees,
    degree_kind,
    education_facts,
    expected_graduation,
    highest_completed_level,
    years_of_experience,
)
from jobhunt.fit.requirements import required_years, split_sections
from jobhunt.models import Answers, Preferences, Profile
from jobhunt.models.config import FitConfig

Result = Literal["pass", "pass_unverified", "unclear", "fail"]
Route = Literal["user", "ai"]


@dataclass
class GateResult:
    gate: str
    result: Result
    reason: str
    evidence: str | None = None
    route: Route | None = None  # for unclear: who resolves it

    def as_dict(self) -> dict[str, object]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Context:
    prefs: Preferences
    answers: Answers
    profile: Profile
    cfg: FitConfig
    today: date


def _snip(text: str, m: re.Match[str], width: int = 80) -> str:
    a, b = max(0, m.start() - width), min(len(text), m.end() + width)
    s = re.sub(r"\s+", " ", text[a:b]).strip()
    return ("…" if a else "") + s + ("…" if b < len(text) else "")


def _required_text(job: JobView) -> str:
    """Posting text minus preferred/bonus sections (those are never knockouts)."""
    keep = []
    for sec in split_sections(job.description):
        if sec.kind == "preferred":
            continue
        keep += sec.lines
    return "\n".join([job.title, *keep])


def _word_in(term: str, text: str) -> re.Match[str] | None:
    return re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", text, re.I)


# --- preference gates (Must only) -----------------------------------------------------------


def gate_blocklist(job: JobView, ctx: Context) -> GateResult | None:
    block = [b for b in ctx.prefs.company.blocklist if b.strip()]
    if not block:
        return None
    for b in block:
        if b.strip().lower() == job.company.strip().lower() or _word_in(b, job.company):
            return GateResult(
                "blocklist", "fail", f"{job.company} is on your blocklist", job.company
            )
    return GateResult("blocklist", "pass", "company not on your blocklist")


def gate_title_keywords(job: JobView, ctx: Context) -> list[GateResult]:
    out = []
    ex = ctx.prefs.roles.exclude_keywords
    if ex and ex.must and ex.value:
        hit = next((k for k in ex.value if _word_in(k, job.title)), None)
        out.append(
            GateResult(
                "exclude_keywords", "fail", f'title contains excluded keyword "{hit}"', job.title
            )
            if hit
            else GateResult("exclude_keywords", "pass", "no excluded keywords in the title")
        )
    inc = ctx.prefs.roles.include_keywords
    if inc and inc.must and inc.value:
        hit = next((k for k in inc.value if _word_in(k, job.title)), None)
        out.append(
            GateResult("include_keywords", "pass", f'title contains "{hit}"', job.title)
            if hit
            else GateResult(
                "include_keywords", "fail", "title has none of your required keywords", job.title
            )
        )
    return out


def gate_seniority(job: JobView, ctx: Context) -> GateResult | None:
    p = ctx.prefs.roles.seniority
    if not (p and p.must and p.value):
        return None
    level = nz.seniority(job.title)
    if level is None:
        return GateResult("seniority", "pass_unverified", "the title doesn't state a level")
    if nz.seniority_ok(level, list(p.value)):
        return GateResult("seniority", "pass", f"{level.replace('_', ' ')} role", job.title)
    return GateResult(
        "seniority",
        "fail",
        f"{level.replace('_', ' ')} role; you want {', '.join(p.value)}",
        job.title,
    )


def gate_employment_type(job: JobView, ctx: Context) -> GateResult | None:
    p = ctx.prefs.employment_types
    if not (p and p.must and p.value):
        return None
    kind = nz.employment_type(job.employment_type, job.title)
    if kind is None:
        return GateResult("employment_type", "pass_unverified", "the posting doesn't say")
    ev = job.employment_type or job.title
    if kind in p.value:
        return GateResult("employment_type", "pass", kind, ev)
    return GateResult("employment_type", "fail", f"{kind}; you want {', '.join(p.value)}", ev)


def gate_term(job: JobView, ctx: Context) -> list[GateResult]:
    out = []
    terms = nz.job_terms(job.terms, job.title, job.description)
    p = ctx.prefs.timing.terms
    if p and p.must and p.value:
        wanted = {t for t in (nz.norm_term(v) for v in p.value) if t}
        if not terms:
            out.append(GateResult("term", "pass_unverified", "the posting doesn't name a term"))
        elif wanted & set(terms):
            out.append(GateResult("term", "pass", ", ".join(terms)))
        else:
            out.append(
                GateResult("term", "fail", f"{', '.join(terms)}; you want {', '.join(p.value)}")
            )
    es, le = ctx.prefs.timing.earliest_start, ctx.prefs.timing.latest_end
    windows = [w for w in (nz.term_window(t) for t in terms) if w]
    if (es and es.must) or (le and le.must):
        if not windows:
            out.append(GateResult("timing", "pass_unverified", "no dates in the posting"))
        else:
            ok = any(
                (not (es and es.must) or w[1] >= es.value)
                and (not (le and le.must) or w[0] <= le.value)
                for w in windows
            )
            out.append(
                GateResult("timing", "pass", "dates fit your window")
                if ok
                else GateResult("timing", "fail", f"{', '.join(terms)} is outside your dates")
            )
    return out


def job_countries(job: JobView) -> set[str]:
    return {c for c in (nz.location_country(loc) for loc in job.locations) if c}


def gate_country(job: JobView, ctx: Context) -> GateResult | None:
    p = ctx.prefs.location.countries
    if not (p and p.must and p.value):
        return None
    countries = job_countries(job)
    allowed = {c.upper() for c in p.value}
    if not countries:
        return GateResult("country", "pass_unverified", "the location doesn't name a country")
    ev = " / ".join(job.locations)
    if countries & allowed:
        return GateResult("country", "pass", ", ".join(sorted(countries & allowed)), ev)
    return GateResult(
        "country",
        "fail",
        f"located in {', '.join(sorted(countries))}; you want {', '.join(sorted(allowed))}",
        ev,
    )


def gate_work_mode(job: JobView, ctx: Context) -> GateResult | None:
    p = ctx.prefs.location.work_modes
    if not (p and p.must and p.value):
        return None
    modes = nz.work_modes(job.work_mode, job.locations, job.title, job.description)
    if not modes:
        return GateResult(
            "work_mode", "pass_unverified", "the posting doesn't say remote/hybrid/onsite"
        )
    allowed = set(p.value)
    if modes & allowed:
        return GateResult("work_mode", "pass", ", ".join(sorted(modes & allowed)))
    if len(modes) > 1:
        return GateResult(
            "work_mode", "unclear", f"the posting mentions {', '.join(sorted(modes))}", route="ai"
        )
    return GateResult(
        "work_mode", "fail", f"{', '.join(modes)}; you want {', '.join(sorted(allowed))}"
    )


def gate_places(job: JobView, ctx: Context) -> GateResult | None:
    p = ctx.prefs.location.places
    if not (p and p.must and p.value):
        return None
    modes = nz.work_modes(job.work_mode, job.locations, job.title, job.description)
    wm = ctx.prefs.location.work_modes
    if "remote" in modes and (wm is None or "remote" in wm.value):
        return GateResult("places", "pass", "remote role")
    unknown = False
    for loc in job.locations:
        for place in p.value:
            if nz.city_name(loc) == nz.city_name(place.name):
                return GateResult("places", "pass", f"in {place.name}", loc)
            d = nz.distance_miles(loc, place.name)
            if d is None:
                unknown = True
            elif d <= (place.radius_miles or 25):
                return GateResult("places", "pass", f"{d:.0f} miles from {place.name}", loc)
    if not job.locations or unknown:
        return GateResult("places", "pass_unverified", "distance to your places is unknown")
    return GateResult(
        "places", "fail", "not within your locations' radius", " / ".join(job.locations)
    )


def gate_pay(job: JobView, ctx: Context) -> GateResult | None:
    p = ctx.prefs.pay
    if not (p and p.must and p.value.min is not None):
        return None
    lo, hi, note = nz.annual_pay(job.pay)
    if lo is None and hi is None:
        return GateResult("pay", "pass_unverified", "pay not listed (flagged, never excluded)")
    floor = nz.to_annual(p.value.min, p.value.unit)
    top = hi if hi is not None else lo
    assert floor is not None and top is not None
    reason = f"top of range {top:,.0f}/yr vs your minimum {floor:,.0f}/yr"
    if note:
        reason += f" ({note})"
    return GateResult("pay", "pass" if top >= floor else "fail", reason)


def gate_avoid_industries(job: JobView, ctx: Context) -> GateResult | None:
    p = ctx.prefs.company.avoid_industries
    if not (p and p.must and p.value):
        return None
    for term in p.value:
        if m := _word_in(term, f"{job.company}\n{job.title}"):
            return GateResult(
                "avoid_industries", "fail", f'"{term}" in company or title', _snip(job.title, m)
            )
        if m := _word_in(term, job.description):
            return GateResult(
                "avoid_industries",
                "unclear",
                f'posting mentions "{term}"',
                _snip(job.description, m),
                route="ai",
            )
    return GateResult("avoid_industries", "pass_unverified", "no avoided industry named")


# --- knockout gates (always on; posting requirement vs the user's facts) --------------------

_NO_SPONSOR = re.compile(
    r"(unable|not able|cannot|can ?not|won'?t|will not|do(?:es)? not|are not able)\s+(?:to\s+)?"
    r"(?:provide\s+|offer\s+|support\s+)?(?:visa\s+|immigration\s+|employment\s+)?sponsor(?:ship)?|"
    r"\bno\s+(?:visa\s+|immigration\s+)?sponsorship\b|"
    r"sponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided|possible)|"
    r"without\s+(?:the\s+need\s+for\s+|requiring\s+|needing\s+)?(?:current\s+or\s+future\s+|future\s+)?"
    r"(?:visa\s+|employer\s+|employment\s+|immigration\s+)?sponsorship|"
    r"not\s+eligible\s+for\s+(?:visa\s+)?sponsorship",
    re.I,
)
_MAYBE_SPONSOR = re.compile(
    r"sponsorship\s+(?:may|might)\s|limited\s+(?:visa\s+)?sponsorship|"
    r"sponsorship\s+(?:is\s+)?available\s+for\s+(?:some|certain|select)|case[- ]by[- ]case",
    re.I,
)
_CITIZEN = re.compile(
    r"(?:u\.?s\.?|united states|us)\s+citizen(?:ship)?\s+(?:is\s+)?(?:required|only)|"
    r"must\s+be\s+an?\s+(?:u\.?s\.?|united states|us)\s+citizen|"
    r"requires?\s+(?:u\.?s\.?|united states|us)\s+citizenship|\bcitizens\s+only\b|"
    r"citizenship\s+(?:is\s+)?required",
    re.I,
)
_US_PERSON = re.compile(r"\bu\.?s\.?\s+persons?\b|export[- ]control|\bitar\b|\bear\b", re.I)
_CLEARANCE = re.compile(
    r"(?:active|current|existing|must\s+(?:have|hold|possess))\s+(?:an?\s+)?"
    r"(?:ts/sci|top\s+secret|secret|security|dod)\s+clearance|"
    r"(?:security\s+)?clearance\s+(?:is\s+)?required",
    re.I,
)
_CLEARANCE_OBTAIN = re.compile(
    r"(?:ability|able|eligib\w+)\s+to\s+obtain\s+(?:and\s+maintain\s+)?(?:an?\s+)?"
    r"(?:ts/sci|top\s+secret|secret|security|government)\s+clearance",
    re.I,
)


def _countries_to_check(job: JobView, ctx: Context) -> list[str]:
    answered = [c.upper() for c in ctx.answers.work_authorization]
    jc = job_countries(job)
    if jc:
        return [c for c in answered if c in jc] or sorted(jc)
    return answered or ["US"]


def _needs_sponsorship(ctx: Context, country: str) -> bool | None:
    wa = ctx.answers.work_authorization.get(country)
    if wa is None:
        return None
    if wa.authorized == "no" or wa.sponsorship_now == "yes" or wa.sponsorship_future == "yes":
        return True
    if wa.authorized == "yes" and wa.sponsorship_now == "no" and wa.sponsorship_future == "no":
        return False
    return None


def gate_sponsorship(job: JobView, ctx: Context) -> GateResult:
    text = _required_text(job)
    m = _NO_SPONSOR.search(text)
    if not m:
        if mm := _MAYBE_SPONSOR.search(text):
            needs = [_needs_sponsorship(ctx, c) for c in _countries_to_check(job, ctx)]
            if all(n is False for n in needs):
                return GateResult(
                    "sponsorship", "pass", "you don't need sponsorship", _snip(text, mm)
                )
            return GateResult(
                "sponsorship",
                "unclear",
                "the posting is vague about sponsorship",
                _snip(text, mm),
                route="ai",
            )
        return GateResult(
            "sponsorship", "pass_unverified", "the posting doesn't mention sponsorship"
        )
    ev = _snip(text, m)
    needs = {c: _needs_sponsorship(ctx, c) for c in _countries_to_check(job, ctx)}
    if any(v is False for v in needs.values()):
        return GateResult("sponsorship", "pass", "no sponsorship offered; you don't need it", ev)
    if needs and all(v is True for v in needs.values()):
        return GateResult(
            "sponsorship", "fail", "no sponsorship offered, and your answers say you need it", ev
        )
    return GateResult(
        "sponsorship",
        "unclear",
        "no sponsorship offered; answer the work-authorization questions in the form",
        ev,
        route="user",
    )


def gate_citizenship(job: JobView, ctx: Context) -> GateResult:
    text = _required_text(job)
    m = _CITIZEN.search(text)
    person = _US_PERSON.search(text) if not m else None
    if not (m or person):
        return GateResult("citizenship", "pass_unverified", "no citizenship requirement stated")
    ev = _snip(text, m or person)  # type: ignore[arg-type]
    wa = ctx.answers.work_authorization.get("US")
    if wa and wa.citizen == "yes":
        return GateResult("citizenship", "pass", "you're a citizen", ev)
    if m:
        if wa and (
            wa.citizen == "no" or wa.sponsorship_now == "yes" or wa.sponsorship_future == "yes"
        ):
            return GateResult("citizenship", "fail", "requires U.S. citizenship", ev)
        return GateResult(
            "citizenship",
            "unclear",
            "requires citizenship; answer the citizenship question in the form",
            ev,
            route="user",
        )
    return GateResult(
        "citizenship",
        "unclear",
        "export-control role (U.S. persons only); check your status",
        ev,
        route="user",
    )


def gate_clearance(job: JobView, ctx: Context) -> GateResult:
    text = _required_text(job)
    have = (ctx.answers.standard.security_clearance or "").strip().lower()
    has_clearance = bool(have) and have not in ("none", "no", "n/a")
    if m := _CLEARANCE.search(text):
        ev = _snip(text, m)
        if has_clearance:
            return GateResult("clearance", "pass", "you hold a clearance", ev)
        if have in ("none", "no", "n/a"):
            return GateResult("clearance", "fail", "requires an active security clearance", ev)
        return GateResult(
            "clearance",
            "unclear",
            "requires a clearance; answer the clearance question",
            ev,
            route="user",
        )
    if m := _CLEARANCE_OBTAIN.search(text):
        ev = _snip(text, m)
        wa = ctx.answers.work_authorization.get("US")
        if has_clearance or (wa and wa.citizen == "yes"):
            return GateResult("clearance", "pass", "eligible to obtain a clearance", ev)
        if wa and (wa.citizen == "no" or wa.sponsorship_future == "yes"):
            return GateResult(
                "clearance", "fail", "clearance eligibility usually requires citizenship", ev
            )
        return GateResult(
            "clearance", "unclear", "must be able to obtain a clearance", ev, route="user"
        )
    return GateResult("clearance", "pass_unverified", "no clearance requirement stated")


MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"  # noqa: E501
_GRAD_RANGE = re.compile(
    rf"graduat\w*\s+(?:date\s+)?(?:between|from|in)?\s*(?:{_MON}\.?\s+)?(20\d\d)\s*(?:and|to|-|–|through)\s*(?:{_MON}\.?\s+)?(20\d\d)",
    re.I,
)
_GRAD_SINGLE = re.compile(
    rf"(?:graduat\w*|graduation(?:\s+date)?)\s+(?:date\s+)?(in|by|of|no later "
    rf"than|before)?\s*(?:{_MON}\.?\s+)?(20\d\d)|class\s+of\s+(20\d\d)",
    re.I,
)
STANDINGS = {"freshman": 1, "first year": 1, "sophomore": 2, "second year": 2, "junior": 3,
             "third year": 3, "senior": 4, "fourth year": 4, "final year": 4,
             # graduate students are never an undergraduate class year
             "graduate": 5, "graduate student": 5, "grad": 5, "master": 5, "masters": 5, "phd": 6,
             "doctoral": 6}  # fmt: skip
_STANDING_RE = re.compile(
    r"\b(rising\s+)?(freshm[ae]n|first[- ]year|sophomores?|second[- ]year|juniors?|third[- ]year|"
    r"seniors?|fourth[- ]year|final[- "
    r"]year)(?:\s+(?:or|and|/|,)\s+(rising\s+)?(freshm[ae]n|first[- ]year|"
    r"sophomores?|second[- ]year|juniors?|third[- ]year|seniors?|fourth[- ]year|final[- ]year))*"
    r"(?=[^.\n]{0,40}\b(students?|undergrad\w*|standing|candidates?|in college|year)\b)",
    re.I,
)


def _month(s: str | None) -> int:
    return MONTHS.get((s or "jun")[:3].lower(), 6)


def _standing_num(word: str) -> int | None:
    w = word.lower().strip().replace("-", " ")
    if w in ("freshman", "freshmen"):
        w = "freshman"
    elif w.endswith("s"):
        w = w[:-1]
    return STANDINGS.get(w)


def gate_graduation(job: JobView, ctx: Context) -> GateResult:
    text = _required_text(job)
    grad = expected_graduation(ctx.profile, ctx.answers, ctx.today)
    if m := _GRAD_RANGE.search(text):
        lo = date(int(m.group(2)), _month(m.group(1)) if m.group(1) else 1, 1)
        hi = date(int(m.group(4)), _month(m.group(3)) if m.group(3) else 12, 1)
        ev = _snip(text, m)
        if grad is None:
            return GateResult(
                "graduation", "unclear", "add your graduation date to the profile", ev, route="user"
            )
        if lo <= grad <= hi:
            return GateResult("graduation", "pass", f"you graduate {grad:%b %Y}", ev)
        return GateResult(
            "graduation", "fail", f"you graduate {grad:%b %Y}, outside {lo:%b %Y}–{hi:%b %Y}", ev
        )
    if m := _GRAD_SINGLE.search(text):
        ev = _snip(text, m)
        year = int(m.group(4) or m.group(3))  # groups: word, month, year | class-of year
        if grad is None:
            return GateResult(
                "graduation", "unclear", "add your graduation date to the profile", ev, route="user"
            )
        word = (m.group(1) or "").lower()
        if m.group(4):  # "class of Y": the academic year ending that summer
            ok = date(year - 1, 9, 1) <= grad <= date(year, 8, 31)
        elif word in ("by", "no later than", "before"):
            ok = grad.year <= year
        else:
            ok = grad.year == year
        return GateResult(
            "graduation",
            "pass" if ok else "fail",
            f"you graduate {grad:%b %Y}; posting asks {year}",
            ev,
        )
    if m := _STANDING_RE.search(text):
        ev = _snip(text, m)
        rising = bool(re.match(r"\s*rising\b", m.group(0), re.I))  # applies to every year listed
        allowed = set()
        for part in re.finditer(r"([a-z]+(?:[- ]year)?)", m.group(0), re.I):
            n = _standing_num(part.group(1))
            if n:
                allowed.add(n - 1 if rising else n)
        mine = _standing_num(ctx.answers.education.class_standing or "")
        if not allowed:
            return GateResult("graduation", "pass_unverified", "no class year stated")
        if mine is None:
            return GateResult(
                "graduation", "unclear", "set your class standing in the form", ev, route="user"
            )
        if mine in allowed:
            return GateResult("graduation", "pass", "your class year matches", ev)
        return GateResult("graduation", "fail", "the posting is for a different class year", ev)
    return GateResult("graduation", "pass_unverified", "no graduation window or class year stated")


_PURSUING = re.compile(
    r"(?:currently\s+)?(?:pursuing|enrolled\s+in|working\s+(?:toward|towards)|studying\s+for)\s+"
    r"(?:an?\s+)?((?:(?:bachelor'?s?|master'?s?|ph\.?\s?d\.?|doctoral|doctorate|undergraduate|graduate|"
    r"b\.?s\.?|m\.?s\.?|ba|ma|mba)\b(?:\s*(?:,|/|\bor\b|\band\b)\s*)?)+)",
    re.I,
)
_REQUIRED_DEGREE = re.compile(
    r"(bachelor'?s?|master'?s?|ph\.?\s?d\.?|doctorate)\s+(?:degree\s+)?(?:\w+\s+){0,6}?(?:is\s+)?required|"
    r"(?:requires?|must\s+have|must\s+hold)\s+an?\s+(bachelor'?s?|master'?s?|ph\.?\s?d\.?|doctorate)",
    re.I,
)


def _kinds_named(fragment: str) -> set[str]:
    kinds = set()
    for tok in re.split(r"\s*(?:,|/|\bor\b|\band\b)\s*", fragment):
        t = tok.strip().lower()
        if not t:
            continue
        if t.startswith("undergrad"):
            kinds.add("bachelor")
        elif t == "graduate":
            kinds |= {"master", "doctorate"}
        elif k := degree_kind(t):
            kinds.add(k)
    return kinds


def gate_degree(job: JobView, ctx: Context) -> GateResult:
    text = _required_text(job)
    edu = education_facts(ctx.profile, ctx.today)
    if m := _PURSUING.search(text):
        ev = _snip(text, m)
        wanted = _kinds_named(m.group(1))
        mine = {e.kind for e in current_degrees(edu) if e.kind}
        if not wanted:
            return GateResult("degree", "pass_unverified", "degree level not named", ev)
        if not mine:
            return GateResult("degree", "fail", "requires being currently enrolled in a degree", ev)
        if mine & wanted:
            return GateResult(
                "degree", "pass", f"you're pursuing a {'/'.join(sorted(mine & wanted))}", ev
            )
        return GateResult(
            "degree",
            "fail",
            f"for {'/'.join(sorted(wanted))} students; you're pursuing {'/'.join(sorted(mine))}",
            ev,
        )
    if m := _REQUIRED_DEGREE.search(text):
        ev = _snip(text, m)
        if re.search(
            r"or equivalent|equivalent (practical |work )?experience",
            text[m.start() : m.end() + 120],
            re.I,
        ):
            return GateResult("degree", "pass_unverified", "degree or equivalent experience", ev)
        need = DEGREE_LEVELS.get(degree_kind(m.group(1) or m.group(2)) or "", 0)
        have = max(
            [highest_completed_level(edu)] + [DEGREE_LEVELS.get(e.kind or "", 0) for e in edu]
        )
        if have >= need:
            return GateResult("degree", "pass", "you have (or will have) that degree", ev)
        return GateResult("degree", "fail", "requires a higher degree than yours", ev)
    if job.degrees:
        mine = {e.kind for e in edu if e.kind}
        listed = {k for k in (degree_kind(d) for d in job.degrees) if k}
        if listed and not (mine & listed):
            return GateResult(
                "degree",
                "unclear",
                f"listing says {', '.join(job.degrees)}",
                ", ".join(job.degrees),
                route="ai",
            )
    return GateResult("degree", "pass_unverified", "no degree requirement stated")


def gate_years(job: JobView, ctx: Context) -> GateResult:
    need, line = required_years(job.description)
    if need is None:
        return GateResult("years_experience", "pass_unverified", "no years of experience stated")
    have = years_of_experience(ctx.profile, ctx.answers, ctx.today)
    ev = (line or "").strip("•-* ")
    if need - have > ctx.cfg.yoe_gate_slack_years:
        return GateResult(
            "years_experience", "fail", f"asks {need:g}+ years; you have about {have:g}", ev
        )
    return GateResult(
        "years_experience", "pass", f"asks {need:g}+ years; you have about {have:g}", ev
    )


LANGUAGES = ["japanese", "mandarin", "chinese", "cantonese", "korean", "german", "french",
             "spanish", "portuguese", "italian", "dutch", "polish", "russian", "arabic", "hebrew",
             "hindi", "vietnamese", "thai", "turkish", "swedish", "norwegian", "danish", "finnish"]  # fmt: skip  # noqa: E501
_LANG = "|".join(LANGUAGES)
_LANG_RE = re.compile(
    rf"(?:fluen(?:t|cy)|proficien\w*|native|business[- ]level|professional[- "
    rf"]level)\s+(?:\w+\s+)?(?:in\s+)?({_LANG})|"
    rf"({_LANG})\s+(?:language\s+)?(?:fluency|proficiency|skills?)\s+(?:is\s+|are\s+)?(?:required|a must|necessary)",  # noqa: E501
    re.I,
)
LANG_ALIASES = {
    "chinese": {"mandarin", "chinese", "cantonese"},
    "mandarin": {"mandarin", "chinese"},
}


def gate_language(job: JobView, ctx: Context) -> GateResult:
    text = _required_text(job)
    m = _LANG_RE.search(text)
    if not m:
        return GateResult("language", "pass_unverified", "no spoken-language requirement")
    lang = (m.group(1) or m.group(2)).lower()
    mine = {s.lower() for s in ctx.answers.skills.spoken_languages}
    ok = bool(LANG_ALIASES.get(lang, {lang}) & mine)
    ev = _snip(text, m)
    if ok:
        return GateResult("language", "pass", f"you speak {lang.capitalize()}", ev)
    if not mine:
        return GateResult(
            "language",
            "unclear",
            f"requires {lang.capitalize()}; list your languages",
            ev,
            route="user",
        )
    return GateResult("language", "fail", f"requires {lang.capitalize()}", ev)


# --- run all --------------------------------------------------------------------------------


def run_gates(job: JobView, ctx: Context) -> list[GateResult]:
    results: list[GateResult | None] = [gate_blocklist(job, ctx)]
    results += gate_title_keywords(job, ctx)
    results += [gate_seniority(job, ctx), gate_employment_type(job, ctx)]
    results += gate_term(job, ctx)
    results += [
        gate_country(job, ctx),
        gate_work_mode(job, ctx),
        gate_places(job, ctx),
        gate_pay(job, ctx),
        gate_avoid_industries(job, ctx),
        gate_sponsorship(job, ctx),
        gate_citizenship(job, ctx),
        gate_clearance(job, ctx),
        gate_graduation(job, ctx),
        gate_degree(job, ctx),
        gate_years(job, ctx),
        gate_language(job, ctx),
    ]
    return [r for r in results if r is not None]


def overall(results: list[GateResult]) -> Literal["passed", "unclear", "failed"]:
    if any(r.result == "fail" for r in results):
        return "failed"
    if any(r.result == "unclear" for r in results):
        return "unclear"
    return "passed"
