"""Built-in canonical questions and where each one's answer lives.

Every key resolves to a value from the profile or a typed answers.yaml section, or to None when
the user hasn't answered it (never a guess). ``sensitive`` keys are never shown to the model.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from jobhunt.bank.ranges import pick_date, pick_number
from jobhunt.fit.normalize import COUNTRY_NAMES
from jobhunt.forms.education import MONTHS, PARTS, month_name, pick_degree, pick_name, year_of
from jobhunt.models import Answers, Preferences, Profile
from jobhunt.models.answers import DECLINE_EEO, WorkAuthorization

Kind = Literal["text", "yesno", "date", "number", "file", "choice", "essay"]
Value = str | list[str] | None


@dataclass
class AnswerContext:
    """What a value can come from, for one job."""

    profile: Profile
    answers: Answers
    prefs: Preferences | None = None
    company: str = ""
    country: str | None = None  # ISO alpha-2 of the job, when known
    resume: Path | None = None

    def work_auth(self) -> WorkAuthorization | None:
        wa = self.answers.work_authorization
        if self.country and self.country in wa:
            return wa[self.country]
        if len(wa) == 1:
            return next(iter(wa.values()))
        return wa.get("US") if not self.country else None


@dataclass(frozen=True)
class KeySpec:
    key: str
    question: str
    kind: Kind
    resolve: Callable[[AnswerContext], Value]
    sensitive: bool = False
    eeo: bool = False
    # extra option texts that mean the same as the value (for selects)
    option_aliases: Callable[[str], list[str]] = field(default=lambda v: [])
    # the option a value confidently means, for values that rarely equal option text
    pick: Callable[[str, list[str]], str | None] | None = None


def _yn(v: str | None) -> str | None:
    return v if v in ("yes", "no") else None


def _either_yes(a: str | None, b: str | None) -> str | None:
    if "yes" in (a, b):
        return "yes"
    if a == "no" and b == "no":
        return "no"
    return None


def _norm_company(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|labs)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _previously_employed(c: AnswerContext) -> str | None:
    known = list(c.answers.experience.prior_employers) + [e.org for e in c.profile.experience]
    if not known or not c.company:
        return None  # the user hasn't listed employers: don't assume "no"
    target = _norm_company(c.company)
    return "yes" if any(_norm_company(k) == target for k in known if k) else "no"


def _full_name(c: AnswerContext) -> str | None:
    i = c.answers.identity
    parts = [i.legal_first_name, i.legal_last_name]
    return " ".join(p for p in parts if p) or None


def _city_region(c: AnswerContext) -> str | None:
    i = c.answers.identity
    parts = [p for p in (i.city, i.region) if p]
    return ", ".join(parts) or None


def country_names(code: str | None) -> list[str]:
    if not code:
        return []
    names = [n for n, cc in COUNTRY_NAMES.items() if cc == code.upper() and len(n) > 3]
    pretty = {"US": "United States", "GB": "United Kingdom"}.get(code.upper())
    return [*([pretty] if pretty else []), *(n.title() for n in names), code.upper()]


def _school(c: AnswerContext) -> str | None:
    return c.profile.education[0].school if c.profile.education else None


def _degree(c: AnswerContext) -> str | None:
    return c.profile.education[0].degree if c.profile.education else None


def _major(c: AnswerContext) -> str | None:
    if c.profile.education and c.profile.education[0].majors:
        return ", ".join(c.profile.education[0].majors)
    return None


def _grad(c: AnswerContext) -> str | None:
    e = c.answers.education
    if e.graduation_year and e.graduation_month:
        return f"{e.graduation_year}-{e.graduation_month:02d}"
    if c.profile.education and c.profile.education[0].graduation:
        return c.profile.education[0].graduation
    return None


def _start(c: AnswerContext) -> str | None:
    return c.profile.education[0].start if c.profile.education else None


def _month_aliases(v: str) -> list[str]:
    """ "May" -> "05", "5", "May": for forms that list months as numbers or short names."""
    if v not in MONTHS:
        return []
    n = MONTHS.index(v) + 1
    return [f"{n:02d}", str(n), v[:3]]


def _gpa(c: AnswerContext) -> str | None:
    e = c.answers.education
    if e.gpa is None:
        return None
    if e.gpa_disclose_at_or_above is not None and e.gpa < e.gpa_disclose_at_or_above:
        return None  # below the user's disclosure threshold: leave it for them
    return f"{e.gpa:g}"


def _current(c: AnswerContext, attr: str) -> str | None:
    exp = c.profile.experience
    cur = next((e for e in exp if e.end is None), exp[0] if exp else None)
    return getattr(cur, attr) if cur else None


def _years(c: AnswerContext) -> str | None:
    y = c.answers.experience.years
    return None if y is None else f"{y:g}"


def _date(v: object) -> str | None:
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else None


def _yes_aliases(v: str) -> list[str]:
    return {"yes": ["Yes", "Y", "True"], "no": ["No", "N", "False"]}.get(v, [])


DECLINE_ALIASES = [
    DECLINE_EEO,
    "Decline to Self Identify",
    "I don't wish to answer",
    "I do not wish to answer",
    "I don't wish to self-identify",
    "Prefer not to say",
    "Prefer not to answer",
    "Decline to state",
    "Decline to answer",
    "I choose not to disclose",
]


def _eeo_aliases(v: str) -> list[str]:
    return DECLINE_ALIASES if v == DECLINE_EEO else []


def _eeo(attr: str) -> Callable[[AnswerContext], Value]:
    return lambda c: getattr(c.answers.eeo, attr)


def _wa(attr: str) -> Callable[[AnswerContext], Value]:
    def get(c: AnswerContext) -> Value:
        wa = c.work_auth()
        return _yn(getattr(wa, attr)) if wa else None

    return get


def _std(attr: str) -> Callable[[AnswerContext], Value]:
    return lambda c: getattr(c.answers.standard, attr)


def _links(attr: str) -> Callable[[AnswerContext], Value]:
    def get(c: AnswerContext) -> Value:
        v = getattr(c.answers.links, attr)
        if v:
            return v
        return next((lk.url for lk in c.profile.links if lk.label.lower() == attr), None)

    return get


def _spec(
    key: str,
    question: str,
    kind: Kind,
    resolve: Callable[[AnswerContext], Value],
    *,
    sensitive: bool = False,
    eeo: bool = False,
    aliases: Callable[[str], list[str]] | None = None,
    pick: Callable[[str, list[str]], str | None] | None = None,
) -> KeySpec:
    if aliases is None:
        aliases = _yes_aliases if kind == "yesno" else (lambda v: [])
    return KeySpec(key, question, kind, resolve, sensitive, eeo, aliases, pick)


SPECS: list[KeySpec] = [
    # identity and contact (filled by code, never shown to the model)
    _spec("identity.first_name", "First name", "text", lambda c: c.answers.identity.legal_first_name, sensitive=True),
    _spec("identity.last_name", "Last name", "text", lambda c: c.answers.identity.legal_last_name, sensitive=True),
    _spec("identity.full_name", "Full name", "text", _full_name, sensitive=True),
    _spec("identity.preferred_name", "Preferred name", "text", lambda c: c.answers.identity.preferred_name or c.answers.identity.legal_first_name, sensitive=True),
    _spec("identity.pronouns", "Pronouns", "choice", lambda c: c.answers.identity.pronouns, sensitive=True),
    _spec("identity.email", "Email", "text", lambda c: c.answers.identity.email, sensitive=True),
    _spec("identity.phone", "Phone", "text", lambda c: c.answers.identity.phone, sensitive=True),
    _spec("identity.address", "Street address", "text", lambda c: c.answers.identity.address_line1, sensitive=True),
    _spec("identity.city_region", "City and state of residence", "text", _city_region, sensitive=True),
    _spec("identity.location", "Current location", "text", _city_region, sensitive=True),
    _spec("identity.postal_code", "Postal code", "text", lambda c: c.answers.identity.postal_code, sensitive=True),
    _spec("identity.country", "Country of residence", "choice", lambda c: c.answers.identity.country, sensitive=True),
    _spec("identity.over_18", "Are you at least 18 years old?", "yesno", lambda c: _yn(c.answers.identity.over_18), sensitive=True),
    # links
    _spec("links.linkedin", "LinkedIn profile", "text", _links("linkedin")),
    _spec("links.github", "GitHub", "text", _links("github")),
    _spec("links.portfolio", "Website or portfolio", "text", _links("portfolio")),
    # files
    _spec("files.resume", "Resume/CV", "file", lambda c: str(c.resume) if c.resume else None),
    _spec("files.cover_letter", "Cover letter", "essay", lambda c: None),
    # education
    _spec("education.school", "School or university", "choice", _school, pick=pick_name),
    _spec("education.degree", "Degree", "choice", _degree, pick=pick_degree),
    _spec("education.major", "Major or field of study", "choice", _major, pick=pick_name),
    _spec("education.graduation", "Graduation date", "date", _grad, pick=pick_date),
    _spec("education.start_month", "Education start month", "choice", lambda c: month_name(_start(c)), aliases=_month_aliases),
    _spec("education.start_year", "Education start year", "text", lambda c: year_of(_start(c))),
    _spec("education.end_month", "Education end (graduation) month", "choice", lambda c: month_name(_grad(c)), aliases=_month_aliases),
    _spec("education.end_year", "Education end (graduation) year", "text", lambda c: year_of(_grad(c))),
    _spec("education.graduation_year", "Graduation year", "choice", lambda c: str(c.answers.education.graduation_year) if c.answers.education.graduation_year else None, pick=pick_number),
    _spec("education.gpa", "GPA", "number", _gpa, sensitive=True, pick=pick_number),
    _spec("education.class_standing", "Current year in school", "choice", lambda c: c.answers.education.class_standing),
    # work authorization (per the job's country)
    _spec("work_auth.authorized", "Are you legally authorized to work in this country?", "yesno", _wa("authorized"), sensitive=True),
    _spec("work_auth.sponsorship_now", "Do you require visa sponsorship now?", "yesno", _wa("sponsorship_now"), sensitive=True),
    _spec("work_auth.sponsorship_future", "Will you require visa sponsorship in the future?", "yesno", _wa("sponsorship_future"), sensitive=True),
    _spec("work_auth.sponsorship", "Will you now or in the future require sponsorship?", "yesno", lambda c: _either_yes(_wa("sponsorship_now")(c), _wa("sponsorship_future")(c)), sensitive=True),  # type: ignore[arg-type]
    _spec("work_auth.citizen", "Are you a citizen of this country?", "yesno", _wa("citizen"), sensitive=True),
    # availability
    _spec("availability.start_date", "Earliest start date", "date", lambda c: _date(c.answers.availability.start_date), pick=pick_date),
    _spec("availability.end_date", "End date", "date", lambda c: _date(c.answers.availability.end_date), pick=pick_date),
    _spec("availability.full_term", "Are you available for the full term?", "yesno", lambda c: _yn(c.answers.availability.full_term)),
    _spec("availability.hours_per_week", "Hours available per week", "number", lambda c: str(c.answers.availability.hours_per_week) if c.answers.availability.hours_per_week else None, pick=pick_number),
    _spec("availability.notice_period", "Notice period", "text", lambda c: c.answers.availability.notice_period),
    # location
    _spec("location.relocate", "Are you willing to relocate?", "yesno", lambda c: _yn(c.answers.location.willing_to_relocate)),
    _spec("location.travel", "Are you willing to travel?", "choice", lambda c: c.answers.location.willing_to_travel),
    # compensation
    _spec("compensation.expected", "Expected compensation", "text", lambda c: "Prefer not to say" if c.answers.compensation.prefer_not_to_say else c.answers.compensation.expected, sensitive=True, pick=pick_number),
    # experience
    _spec("experience.years", "Years of experience", "number", _years, pick=pick_number),
    _spec("experience.current_employer", "Current or most recent employer", "text", lambda c: _current(c, "org")),
    _spec("experience.current_title", "Current or most recent job title", "text", lambda c: _current(c, "title")),
    _spec("experience.previously_employed", "Have you worked for this company before?", "yesno", _previously_employed),
    # skills
    _spec("skills.languages", "Languages you speak", "choice", lambda c: list(c.answers.skills.spoken_languages) or None),
    # standard yes/no and consent
    _spec("standard.relatives", "Do you have relatives working at this company?", "yesno", lambda c: _yn(c.answers.standard.relatives_at_company), sensitive=True),
    _spec("standard.non_compete", "Are you bound by a non-compete agreement?", "yesno", lambda c: _yn(c.answers.standard.non_compete), sensitive=True),
    _spec("standard.clearance", "Do you hold a security clearance?", "choice", _std("security_clearance"), sensitive=True),
    _spec("standard.background_check", "Do you consent to a background check?", "yesno", lambda c: _yn(c.answers.standard.background_check_consent), sensitive=True),
    _spec("standard.drug_test", "Do you consent to a drug test?", "yesno", lambda c: _yn(c.answers.standard.drug_test_consent), sensitive=True),
    _spec("standard.sms_consent", "Do you consent to text messages?", "yesno", lambda c: _yn(c.answers.standard.sms_consent)),
    # sourcing
    _spec("sourcing.how_did_you_hear", "How did you hear about us?", "choice", lambda c: c.answers.sourcing.how_did_you_hear),
    _spec("sourcing.referral", "Were you referred by an employee?", "text", lambda c: c.answers.sourcing.referral_default),
    # voluntary self-identification
    _spec("eeo.gender", "Gender", "choice", _eeo("gender"), sensitive=True, eeo=True, aliases=_eeo_aliases),
    _spec("eeo.race", "Race", "choice", _eeo("race_ethnicity"), sensitive=True, eeo=True, aliases=_eeo_aliases),
    _spec("eeo.hispanic", "Are you Hispanic or Latino?", "choice", _eeo("hispanic_latino"), sensitive=True, eeo=True, aliases=_eeo_aliases),
    _spec("eeo.veteran", "Veteran status", "choice", _eeo("veteran_status"), sensitive=True, eeo=True, aliases=_eeo_aliases),
    _spec("eeo.disability", "Disability status", "choice", _eeo("disability_status"), sensitive=True, eeo=True, aliases=_eeo_aliases),
    _spec("eeo.sexual_orientation", "Sexual orientation", "choice", _eeo("sexual_orientation"), sensitive=True, eeo=True, aliases=_eeo_aliases),
    _spec("eeo.transgender", "Do you identify as transgender?", "choice", _eeo("transgender"), sensitive=True, eeo=True, aliases=_eeo_aliases),
    # essays: never filled from the bank; detected so the job is classed Essay
    _spec("essay.why_company", "Why do you want to work here?", "essay", lambda c: None),
    _spec("essay.project", "Tell us about a project you're proud of", "essay", lambda c: None),
    _spec("essay.additional", "Anything else you'd like to share?", "essay", lambda c: None),
]  # fmt: skip

BUILTIN: dict[str, KeySpec] = {s.key: s for s in SPECS}

# Standard ATS field names that map to a key without reading the label.
FIELD_NAMES: dict[str, str] = {
    # Greenhouse
    "first_name": "identity.first_name",
    "last_name": "identity.last_name",
    "preferred_name": "identity.preferred_name",
    "email": "identity.email",
    "phone": "identity.phone",
    "resume": "files.resume",
    "resume_text": "files.resume",
    "cover_letter": "files.cover_letter",
    "cover_letter_text": "files.cover_letter",
    "location": "identity.location",
    "gender": "eeo.gender",
    "race": "eeo.race",
    "hispanic_ethnicity": "eeo.hispanic",
    "veteran_status": "eeo.veteran",
    "disability_status": "eeo.disability",
    # the first entry of the Education block (forms/education.py)
    **{f"{p.dom}--0": p.key for p in PARTS},
    # Lever
    "name": "identity.full_name",
    "org": "experience.current_employer",
    "urls[linkedin]": "links.linkedin",
    "urls[github]": "links.github",
    "urls[portfolio]": "links.portfolio",
    "urls[website]": "links.portfolio",
    "comments": "essay.additional",
    "eeo[gender]": "eeo.gender",
    "eeo[race]": "eeo.race",
    "eeo[veteran]": "eeo.veteran",
    "eeo[disability]": "eeo.disability",
    # Ashby system fields
    "_systemfield_name": "identity.full_name",
    "_systemfield_email": "identity.email",
    "_systemfield_resume": "files.resume",
    "_systemfield_location": "identity.location",
    "_systemfield_phone": "identity.phone",
    "_systemfield_eeoc_gender": "eeo.gender",
    "_systemfield_eeoc_race": "eeo.race",
    "_systemfield_eeoc_veteran_status": "eeo.veteran",
    "_systemfield_eeoc_disability_status": "eeo.disability",
    "communicationconsent": "standard.sms_consent",
}

SENSITIVE_LABEL_RE = re.compile(
    r"\b(authori[sz]ed|authori[sz]ation|sponsor|visa|citizen|permanent resident|green card|"
    r"immigration|f-?1\b|j-?1\b|h-?1b|opt\b|cpt\b|work permit|nationality|"
    r"clearance|salary|compensation|pay expectation|gender|race|ethnic|hispanic|latino|"
    r"veteran|disabilit|sexual orientation|transgender|criminal|convicted|felony|background "
    r"check|drug test|date of birth|social security|ssn|age\b|18 years)",
    re.I,
)


def is_sensitive_label(label: str) -> bool:
    return bool(SENSITIVE_LABEL_RE.search(label))
