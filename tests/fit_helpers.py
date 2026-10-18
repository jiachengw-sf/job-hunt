"""Builders shared by the fit tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from jobhunt.config import load_config
from jobhunt.fit import gates as gt
from jobhunt.fit.evaluate import _load_persona
from jobhunt.fit.jobview import JobView
from jobhunt.paths import Paths
from tests.conftest import PERSONAS_DIR

TODAY = date(2026, 10, 18)


def persona_ctx(name: str, tmp: Path | None = None, **answer_overrides: object) -> gt.Context:
    profile, prefs, answers = _load_persona(PERSONAS_DIR / name)
    cfg = load_config(Paths(tmp or Path("/nonexistent-jobhunt-home"))).fit
    return gt.Context(prefs, answers, profile, cfg, TODAY)


def job(
    title: str = "Software Engineering Intern, Summer 2027",
    description: str = "",
    *,
    locations: list[str] | None = None,
    company: str = "Example Co",
    work_mode: str | None = None,
    employment_type: str | None = None,
    pay: dict | None = None,
    jid: int = 1,
) -> JobView:
    return JobView(
        id=jid,
        company=company,
        title=title,
        locations=locations if locations is not None else ["Chicago, IL"],
        description=description,
        ats="greenhouse",
        work_mode=work_mode,
        employment_type=employment_type,
        pay=pay,
        posted_at="2026-10-15T00:00:00Z",
        first_seen="2026-10-15T00:00:00Z",
    )


def gate(results: list[gt.GateResult], name: str) -> gt.GateResult:
    return next(r for r in results if r.gate == name)
