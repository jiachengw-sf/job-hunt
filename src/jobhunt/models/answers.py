"""answers.yaml: the screening questionnaire and the answer bank.

Sensitive answers are entered by the user in the local form only, are filled by code, and are never
shown to the model. Every field may be left unset (skipped); an unset field is never guessed.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from jobhunt.models.base import Strict

YesNo = Literal["yes", "no"]
YesNoDecline = Literal["yes", "no", "decline"]
DECLINE_EEO = "Decline to self-identify"


class Identity(Strict):
    legal_first_name: str | None = None
    legal_last_name: str | None = None
    preferred_name: str | None = None
    pronouns: str | None = None
    email: str | None = None
    phone: str | None = None
    phone_country: str | None = Field(None, description="ISO alpha-2, set before the number")
    school_email: str | None = Field(None, description="University email, apart from your own.")
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country: str | None = None
    over_18: YesNo | None = None


class Links(Strict):
    linkedin: str | None = None
    github: str | None = None
    portfolio: str | None = None
    other: list[str] = Field(default_factory=list)


class EducationAnswers(Strict):
    class_standing: str | None = Field(None, description="e.g. Freshman, Sophomore, Senior")
    graduation_month: int | None = Field(None, ge=1, le=12)
    graduation_year: int | None = Field(None, ge=1950, le=2100)
    gpa: float | None = Field(None, ge=0)
    gpa_scale: float | None = Field(None, gt=0)
    gpa_disclose_at_or_above: float | None = Field(
        None, description="Only fill GPA when it is at least this value."
    )
    high_school_graduation_year: int | None = Field(None, ge=1950, le=2100)
    sat_score: str | None = None
    act_score: str | None = None
    gre_score: str | None = None
    gmat_score: str | None = None
    no_test_scores: bool = Field(False, description="You haven't taken a standardized test.")
    further_study: Literal["yes", "no", "undecided"] | None = Field(
        None, description="Plans for more school after this degree."
    )


class WorkAuthorization(Strict):
    authorized: YesNo | None = None
    citizen: YesNo | None = Field(
        None, description="Citizen of this country (for citizenship-only roles)."
    )
    sponsorship_now: YesNo | None = None
    sponsorship_future: YesNo | None = None
    student_status: str | None = Field(
        None, description="Free text the user wrote, e.g. their student work-authorization status."
    )


class Availability(Strict):
    start_date: date | None = None
    end_date: date | None = None
    terms: list[str] = Field(default_factory=list)
    full_term: YesNo | None = None
    hours_per_week: int | None = None
    notice_period: str | None = None


class LocationAnswers(Strict):
    willing_to_relocate: YesNo | None = None
    willing_to_travel: str | None = None
    relocate_anywhere_in: list[str] = Field(
        default_factory=list,
        description="ISO countries you'd relocate anywhere in: answers city-specific questions.",
    )
    onsite_full_time: YesNo | None = Field(
        None, description="Open to working onsite five days a week, wherever the job is."
    )


class Compensation(Strict):
    expected: str | None = None
    prefer_not_to_say: bool = False


class ExperienceAnswers(Strict):
    years: float | None = Field(None, ge=0)
    prior_employers: list[str] = Field(default_factory=list)


class SkillsAnswers(Strict):
    certifications: list[str] = Field(default_factory=list)
    spoken_languages: list[str] = Field(default_factory=list)


class Standard(Strict):
    relatives_at_company: YesNo | None = None
    non_compete: YesNo | None = None
    security_clearance: str | None = None
    background_check_consent: YesNoDecline | None = None
    drug_test_consent: YesNoDecline | None = None
    sms_consent: YesNo | None = None
    other_offers: YesNo | None = Field(None, description="You currently hold other offers.")


class Sourcing(Strict):
    how_did_you_hear: str | None = None
    referral_default: str | None = None


class EEO(Strict):
    """Voluntary. Every field defaults to declining. Until the user reviews this section in the
    local form (``confirmed``), EEO blocks are left for the user to fill on every application."""

    confirmed: bool = False

    gender: str = DECLINE_EEO
    race_ethnicity: str = DECLINE_EEO
    hispanic_latino: str = DECLINE_EEO
    veteran_status: str = DECLINE_EEO
    disability_status: str = DECLINE_EEO
    sexual_orientation: str = DECLINE_EEO
    transgender: str = DECLINE_EEO


class BaseAnswers(Strict):
    """Starting material for essays. Never auto-filled into a form."""

    bio: str | None = None
    why_field: str | None = None
    project: str | None = None


class BankEntry(Strict):
    """A canonical question. For a built-in key (e.g. ``work_auth.sponsorship_future``) the answer
    comes from the typed section above and ``answer`` stays empty; the entry then only records
    the wordings the user confirmed and the option texts they accept."""

    key: str = Field(pattern=r"^[a-z0-9_.]+$", description="Stable canonical key.")
    question: str = Field(description="Canonical wording.")
    answer: str | None = None
    wordings: list[str] = Field(
        default_factory=list, description="Every label the user has confirmed maps to this key."
    )
    options: list[str] = Field(
        default_factory=list, description="Acceptable dropdown option texts, in preference order."
    )
    sensitive: bool = False


class Answers(Strict):
    version: int = 1
    identity: Identity = Field(default_factory=Identity)
    links: Links = Field(default_factory=Links)
    education: EducationAnswers = Field(default_factory=EducationAnswers)
    work_authorization: dict[str, WorkAuthorization] = Field(
        default_factory=dict, description="Keyed by ISO alpha-2 country code."
    )
    availability: Availability = Field(default_factory=Availability)
    location: LocationAnswers = Field(default_factory=LocationAnswers)
    compensation: Compensation = Field(default_factory=Compensation)
    experience: ExperienceAnswers = Field(default_factory=ExperienceAnswers)
    skills: SkillsAnswers = Field(default_factory=SkillsAnswers)
    standard: Standard = Field(default_factory=Standard)
    sourcing: Sourcing = Field(default_factory=Sourcing)
    eeo: EEO = Field(default_factory=EEO)
    base_answers: BaseAnswers = Field(default_factory=BaseAnswers)
    bank: list[BankEntry] = Field(default_factory=list)
