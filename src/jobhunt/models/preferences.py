"""preferences.yaml. Each preference is Must (a hard gate) or Nice (priority only, the default).

Each preference is written either as ``{value: ..., importance: must|nice}`` or as the bare value
(shorthand for Nice).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Generic, Literal, TypeVar

from pydantic import Field, model_validator

from jobhunt.models.base import Strict

T = TypeVar("T")

Importance = Literal["must", "nice"]
Seniority = Literal["internship", "new_grad", "entry", "mid", "senior"]
EmploymentType = Literal["internship", "co-op", "full-time", "part-time", "contract"]
WorkMode = Literal["onsite", "hybrid", "remote"]


class Pref(Strict, Generic[T]):
    value: T
    importance: Importance = "nice"

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, data: Any) -> Any:
        if isinstance(data, dict) and "value" in data:
            return data
        return {"value": data}

    @property
    def must(self) -> bool:
        return self.importance == "must"


class Place(Strict):
    name: str
    radius_miles: float | None = Field(None, ge=0)


class Pay(Strict):
    min: float | None = Field(None, ge=0)
    ideal: float | None = Field(None, ge=0)
    unit: Literal["hourly", "annual"] = "annual"
    currency: str = "USD"


class Roles(Strict):
    families: Pref[list[str]] | None = None
    include_keywords: Pref[list[str]] | None = None
    exclude_keywords: Pref[list[str]] | None = None
    seniority: Pref[list[Seniority]] | None = None


class Timing(Strict):
    terms: Pref[list[str]] | None = Field(None, description='e.g. ["Summer 2027"]')
    earliest_start: Pref[date] | None = None
    latest_end: Pref[date] | None = None
    min_duration_weeks: Pref[int] | None = None
    max_duration_weeks: Pref[int] | None = None
    hours_per_week: Pref[int] | None = None


class Location(Strict):
    places: Pref[list[Place]] | None = None
    work_modes: Pref[list[WorkMode]] | None = None
    relocation: Pref[bool] | None = None
    countries: Pref[list[str]] | None = Field(None, description="ISO 3166 alpha-2 codes")


class FormFilters(Strict):
    """Jobs whose application forms require something you don't have. Each entry is an answer
    key (``education.sat_score``) or a word in the field's label (``SAT``)."""

    skip_if_required: list[str] = Field(
        default_factory=list, description="Skip: off the Ready list, pre-fill and the digest."
    )
    deprioritize_if_required: list[str] = Field(
        default_factory=list, description="Rank lower (fit.form_filter_factor)."
    )


class Company(Strict):
    sizes: Pref[list[str]] | None = None
    stages: Pref[list[str]] | None = None
    prefer_industries: Pref[list[str]] | None = None
    avoid_industries: Pref[list[str]] | None = None
    blocklist: list[str] = Field(default_factory=list, description="Companies never shown.")


class Preferences(Strict):
    version: int = 1
    roles: Roles = Field(default_factory=Roles)
    employment_types: Pref[list[EmploymentType]] | None = None
    timing: Timing = Field(default_factory=Timing)
    location: Location = Field(default_factory=Location)
    pay: Pref[Pay] | None = Field(
        None, description="Missing pay in a posting is flagged, never excluded."
    )
    company: Company = Field(default_factory=Company)
    dealbreakers: list[str] = Field(default_factory=list, description="Always treated as Must.")
    forms: FormFilters = Field(default_factory=FormFilters)
