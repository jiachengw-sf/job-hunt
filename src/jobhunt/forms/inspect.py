"""Inspect jobs' application forms: read (or reuse) each form spec, store the ATS posting text as
a view, classify against the answer bank, and rebuild new_questions.yaml.

Reading a form is always a GET of a public endpoint or page (Ashby: a headless page load whose
only allowed requests are reads). Nothing is ever submitted.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from jobhunt.bank.keys import AnswerContext, is_sensitive_label
from jobhunt.bank.match import Matcher, company_template, normalize
from jobhunt.discovery.resolver import SUPPORTED, resolve
from jobhunt.fit.gates import job_countries
from jobhunt.fit.jobview import JobView, load_jobs
from jobhunt.forms import ashby, greenhouse, lever
from jobhunt.forms.classify import BLOCKING, Classification, classify, manual
from jobhunt.forms.model import FormError, FormSpec
from jobhunt.http import HttpClient
from jobhunt.models import Answers, Preferences, Profile
from jobhunt.paths import secure_file

SPEC_MAX_AGE_DAYS = 7


class JobForm(BaseModel):
    job_id: int
    company: str
    title: str
    ats: str
    status: str = Field(description="ok | unsupported | error | needs_browser | not_found")
    cls: str = Field(alias="class")
    reasons: list[str] = Field(default_factory=list)
    required: int = 0
    covered: int = 0
    needs: list[str] = Field(default_factory=list, description="Required fields needing you.")
    essays: list[str] = Field(default_factory=list)
    human: list[str] = Field(default_factory=list, description="Always-human fields.")
    cached: bool = False
    description_stored: bool = False

    model_config = {"populate_by_name": True}


class InspectReport(BaseModel):
    dry_run: bool
    inspected: int
    by_class: dict[str, int]
    by_status: dict[str, int]
    fetched: int
    cached: int
    descriptions_stored: int
    new_questions: int = Field(description="Distinct unanswered questions in new_questions.yaml.")
    jobs: list[JobForm]
    warnings: list[str] = Field(default_factory=list)


@dataclass
class Inputs:
    profile: Profile
    answers: Answers
    prefs: Preferences | None
    resume: Path | None
    now: str
    threshold: float = 0.6
    matcher: Matcher = field(init=False)

    def __post_init__(self) -> None:
        self.matcher = Matcher(self.answers, threshold=self.threshold)

    def ctx(self, job: JobView) -> AnswerContext:
        countries = sorted(job_countries(job))
        return AnswerContext(
            self.profile,
            self.answers,
            self.prefs,
            company=job.company,
            country=countries[0] if len(countries) == 1 else None,
            resume=self.resume,
        )


AshbyLoaderFactory = Callable[[], AbstractContextManager[ashby.Loader]]


def _job_row(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT id, ats, board_token, ats_job_id, canonical_url, description FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()


def read_form(
    row: sqlite3.Row, client: HttpClient, ashby_loader: Callable[[], ashby.Loader]
) -> FormSpec:
    ats, board, jid = row["ats"], row["board_token"], row["ats_job_id"]
    if ats not in SUPPORTED:
        raise FormError(
            f"{ats or 'unknown'} isn't a supported ATS: apply by hand with the packet",
            "unsupported",
        )
    if not board or not jid:
        raise FormError(
            "the ATS board for this link is unknown, so its form can't be read", "unsupported"
        )
    if ats == "greenhouse":
        return greenhouse.fetch(client, board, jid)
    if ats == "lever":
        return lever.fetch(client, board, jid, resolve(row["canonical_url"] or "").region)
    return ashby.fetch(ashby_loader(), board, jid)


def _fresh(fetched_at: str, now: str, max_age_days: int) -> bool:
    f = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    n = datetime.fromisoformat(now.replace("Z", "+00:00"))
    return n - f < timedelta(days=max_age_days)


def inspect_jobs(
    conn: sqlite3.Connection,
    job_ids: list[int],
    inp: Inputs,
    client: HttpClient,
    *,
    ashby_loader_factory: AshbyLoaderFactory,
    refresh: bool = False,
    max_age_days: int = SPEC_MAX_AGE_DAYS,
) -> tuple[list[JobForm], dict[str, int]]:
    jobs = {j.id: j for j in load_jobs(conn, job_ids, live_only=False)}
    out: list[JobForm] = []
    stats = {"fetched": 0, "cached": 0, "descriptions": 0}
    eeo_ok = inp.answers.eeo.confirmed
    loader_cm: AbstractContextManager[ashby.Loader] | None = None
    loader: ashby.Loader | None = None

    def get_loader() -> ashby.Loader:
        nonlocal loader_cm, loader
        if loader is None:
            loader_cm = ashby_loader_factory()
            loader = loader_cm.__enter__()
        return loader

    try:
        for jid in job_ids:
            job = jobs.get(jid)
            if job is None:
                continue
            row = _job_row(conn, jid)
            cached = conn.execute("SELECT * FROM form_specs WHERE job_id=?", (jid,)).fetchone()
            spec: FormSpec | None = None
            status, error = "ok", None
            used_cache = False
            if (
                cached
                and not refresh
                and cached["status"] in ("ok", "unsupported")
                and _fresh(cached["fetched_at"], inp.now, max_age_days)
            ):
                used_cache = True
                status, error = cached["status"], cached["error"]
                if cached["spec_json"]:
                    spec = FormSpec.model_validate_json(cached["spec_json"])
            else:
                try:
                    spec = read_form(row, client, get_loader)
                    stats["fetched"] += 1
                except FormError as e:
                    status, error = e.status, str(e)
            stats["cached"] += used_cache
            stored = False
            if spec is not None and not used_cache:
                stored = _store_view(conn, jid, spec, inp.now)
                stats["descriptions"] += stored
            if spec is None:
                c = manual(error or status)
            else:
                c = classify(spec, inp.ctx(job), inp.matcher, eeo_confirmed=eeo_ok)
            _save(conn, jid, row["ats"] or "unknown", spec, status, error, c, inp.now, used_cache)
            out.append(_summary(job, row["ats"] or "unknown", status, c, used_cache, stored))
    finally:
        if loader_cm is not None:
            loader_cm.__exit__(None, None, None)
    return out, stats


def _store_view(conn: sqlite3.Connection, job_id: int, spec: FormSpec, now: str) -> bool:
    """The ATS's own posting text, as a non-polling view (never affects closures)."""
    if not spec.description:
        return False
    conn.execute(
        "INSERT INTO posting_views (job_id, ats, url, description, pay_json, fetched_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET ats=excluded.ats, "
        "url=excluded.url, description=excluded.description, pay_json=excluded.pay_json, "
        "fetched_at=excluded.fetched_at",
        (
            job_id,
            spec.ats,
            spec.url,
            spec.description,
            json.dumps(spec.pay) if spec.pay else None,
            now,
        ),
    )
    return True


def _save(
    conn: sqlite3.Connection,
    job_id: int,
    ats: str,
    spec: FormSpec | None,
    status: str,
    error: str | None,
    c: Classification,
    now: str,
    used_cache: bool,
) -> None:
    details = c.model_dump(mode="json", by_alias=True)
    if used_cache:
        conn.execute(
            "UPDATE form_specs SET class=?, details_json=?, classified_at=? WHERE job_id=?",
            (c.cls, json.dumps(details), now, job_id),
        )
        return
    conn.execute(
        "INSERT INTO form_specs (job_id, ats, url, status, error, spec_json, fetched_at, class, "
        "details_json, classified_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE "
        "SET ats=excluded.ats, url=excluded.url, status=excluded.status, error=excluded.error, "
        "spec_json=excluded.spec_json, fetched_at=excluded.fetched_at, class=excluded.class, "
        "details_json=excluded.details_json, classified_at=excluded.classified_at",
        (
            job_id,
            ats,
            spec.url if spec else None,
            status,
            error,
            spec.model_dump_json() if spec else None,
            now,
            c.cls,
            json.dumps(details),
            now,
        ),
    )
    conn.execute(
        "UPDATE applications SET class=?, updated_at=? WHERE job_id=?", (c.cls, now, job_id)
    )


def _summary(
    job: JobView, ats: str, status: str, c: Classification, cached: bool, stored: bool
) -> JobForm:
    req = c.required
    return JobForm(
        job_id=job.id,
        company=job.company,
        title=job.title,
        ats=ats,
        status=status,
        cls=c.cls,
        reasons=c.reasons,
        required=len(req),
        covered=sum(f.resolution not in BLOCKING and f.resolution != "essay" for f in req),
        needs=[f"{f.label} ({f.resolution})" for f in c.needs],
        essays=[f.label for f in c.essays if f.required],
        human=[f.label for f in c.human],
        cached=cached,
        description_stored=stored,
    )


def reclassify_cached(conn: sqlite3.Connection, inp: Inputs, skip: set[int]) -> int:
    """Re-classify every other cached form against the current answer bank (local, no network),
    so classes and new_questions.yaml reflect answers given since the form was read."""
    rows = conn.execute(
        "SELECT f.job_id, f.spec_json FROM form_specs f JOIN jobs j ON j.id=f.job_id "
        "WHERE j.live=1 AND f.status='ok' AND f.spec_json IS NOT NULL"
    ).fetchall()
    todo = [r for r in rows if r["job_id"] not in skip]
    jobs = {j.id: j for j in load_jobs(conn, [r["job_id"] for r in todo])}
    n = 0
    for r in todo:
        job = jobs.get(r["job_id"])
        if job is None:
            continue
        spec = FormSpec.model_validate_json(r["spec_json"])
        c = classify(spec, inp.ctx(job), inp.matcher, eeo_confirmed=inp.answers.eeo.confirmed)
        conn.execute(
            "UPDATE form_specs SET class=?, details_json=?, classified_at=? WHERE job_id=?",
            (c.cls, json.dumps(c.model_dump(mode="json", by_alias=True)), inp.now, r["job_id"]),
        )
        conn.execute("UPDATE applications SET class=? WHERE job_id=?", (c.cls, r["job_id"]))
        n += 1
    return n


# --- new_questions.yaml -----------------------------------------------------------------------


def unanswered_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Unknown and candidate questions across live inspected jobs, grouped by normalized
    wording (the company name normalized away), most frequent first."""
    groups: dict[str, dict[str, Any]] = {}
    rows = conn.execute(
        "SELECT f.job_id, f.details_json, j.company FROM form_specs f JOIN jobs j ON "
        "j.id=f.job_id WHERE j.live=1 AND f.details_json IS NOT NULL ORDER BY f.job_id"
    ).fetchall()
    for r in rows:
        details = json.loads(r["details_json"])
        for f in details.get("fields") or []:
            if f["resolution"] not in ("unknown", "candidate", "skipped") or (
                f["resolution"] == "skipped" and f.get("match") not in ("unknown", "candidate")
            ):
                continue
            norm = normalize(f["label"], r["company"])
            if not norm:
                continue
            g = groups.setdefault(
                norm,
                {
                    "normalized": norm,
                    "label": company_template(f["label"], r["company"]),
                    "type": f["type"],
                    "jobs": 0,
                    "required_in": 0,
                    "job_ids": [],
                    "options": f.get("options") or [],
                    "candidate_key": f.get("key") if f.get("match") == "candidate" else None,
                    "candidate_score": f.get("score") if f.get("match") == "candidate" else None,
                    "sensitive": bool(f.get("sensitive")) or is_sensitive_label(f["label"]),
                },
            )
            if r["job_id"] not in g["job_ids"]:
                g["jobs"] += 1
                g["job_ids"].append(r["job_id"])
            g["required_in"] += bool(f["required"])
    out = sorted(groups.values(), key=lambda g: (-g["required_in"], -g["jobs"], g["normalized"]))
    for g in out:
        g["job_ids"] = g["job_ids"][:20]
        g["options"] = g["options"][:20]
    return out


def write_new_questions(path: Path, groups: list[dict[str, Any]], now: str) -> None:
    data = {
        "updated_at": now,
        "note": "Questions found on application forms with no confirmed answer yet. Rebuilt by "
        "`jobhunt inspect`; answer them in the local form (`jobhunt setup form`) or confirm "
        "a suggested match with `jobhunt answers confirm`.",
        "questions": groups,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(path)
    secure_file(path)


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def null_loader_factory() -> AbstractContextManager[ashby.Loader]:
    def load(board: str, job_id: str) -> dict[str, Any]:
        raise FormError("Ashby forms need the browser extra", "needs_browser")

    return nullcontext(load)
