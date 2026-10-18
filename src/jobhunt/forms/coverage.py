"""The coverage report: how much of the user's matches the answer bank can fill.

- the share of *required* fields with a confirmed answer, across inspected forms
- the most common unanswered questions (offer to answer them now)
- application classes
- pre-fill coverage by ATS over every recalled match ("62% of your matches are on Workday:
  these will get hand-off packets")
"""

from __future__ import annotations

import json
import sqlite3

from pydantic import BaseModel, Field

from jobhunt.discovery.resolver import SUPPORTED
from jobhunt.forms.classify import BLOCKING
from jobhunt.forms.inspect import JobForm, unanswered_groups


class AtsShare(BaseModel):
    ats: str
    jobs: int
    share: float
    handling: str = Field(description="pre-fill | hand-off packet")


class QuestionCount(BaseModel):
    question: str
    jobs: int
    required_in: int
    suggested_key: str | None = None
    sensitive: bool


class CoverageReport(BaseModel):
    dry_run: bool
    inspected: int
    readable: int = Field(description="Forms read successfully.")
    required_fields: int
    required_covered: int
    coverage: float | None = Field(description="Share of required fields with a confirmed answer.")
    by_class: dict[str, int]
    by_status: dict[str, int]
    top_unanswered: list[QuestionCount]
    essays: int = Field(description="Required free-text answers across the sample.")
    ats_mix: list[AtsShare]
    ats_note: str | None
    jobs: list[JobForm] = Field(default_factory=list)


def sample_jobs(conn: sqlite3.Connection, n: int, *, supported_only: bool = True) -> list[int]:
    """The top recalled, not gated-out, live matches by priority (supported ATSs first)."""
    sql = (
        "SELECT j.id, j.ats FROM jobs j JOIN fits f ON f.job_id=j.id WHERE j.live=1 AND "
        "f.recalled=1 AND coalesce(f.gate_status,'') != 'failed' "
        "ORDER BY coalesce(f.priority, 0) DESC, j.id"
    )
    rows = conn.execute(sql).fetchall()
    if supported_only:
        rows = [r for r in rows if r["ats"] in SUPPORTED]
    return [r["id"] for r in rows[:n]]


def ats_mix(conn: sqlite3.Connection) -> tuple[list[AtsShare], str | None]:
    rows = conn.execute(
        "SELECT coalesce(j.ats,'unknown') AS ats, count(*) AS n FROM jobs j JOIN fits f ON "
        "f.job_id=j.id WHERE j.live=1 AND f.recalled=1 AND coalesce(f.gate_status,'') != "
        "'failed' GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    total = sum(r["n"] for r in rows) or 1
    mix = [
        AtsShare(
            ats=r["ats"],
            jobs=r["n"],
            share=round(r["n"] / total, 3),
            handling="pre-fill" if r["ats"] in SUPPORTED else "hand-off packet",
        )
        for r in rows
    ]
    note = None
    top_manual = next((m for m in mix if m.handling == "hand-off packet"), None)
    if top_manual and top_manual.share >= 0.05:
        manual_share = sum(m.share for m in mix if m.handling == "hand-off packet")
        note = (
            f"{round(top_manual.share * 100)}% of your matches are on {top_manual.ats}"
            f"{' (unrecognized ATS)' if top_manual.ats == 'unknown' else ''}: these will get "
            f"hand-off packets ({round(manual_share * 100)}% of matches in all)."
        )
    return mix, note


def report(
    conn: sqlite3.Connection, job_ids: list[int], jobs: list[JobForm], *, dry_run: bool
) -> CoverageReport:
    req = cov = essays = readable = 0
    by_class: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for jid in job_ids:
        r = conn.execute(
            "SELECT status, class, details_json FROM form_specs WHERE job_id=?", (jid,)
        ).fetchone()
        if not r:
            continue
        by_class[r["class"]] = by_class.get(r["class"], 0) + 1
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        if r["status"] != "ok":
            continue
        readable += 1
        for f in json.loads(r["details_json"]).get("fields") or []:
            if not f["required"]:
                continue
            if f["resolution"] == "essay":
                essays += 1
                continue
            req += 1
            cov += f["resolution"] not in BLOCKING
    in_sample = set(job_ids)
    top: list[QuestionCount] = []
    for g in unanswered_groups(conn):
        n = len([j for j in g["job_ids"] if j in in_sample])
        if not n or not g["required_in"]:
            continue
        top.append(
            QuestionCount(
                question=g["label"],
                jobs=n,
                required_in=g["required_in"],
                suggested_key=g["candidate_key"],
                sensitive=g["sensitive"],
            )
        )
    top.sort(key=lambda q: (-q.jobs, q.question))
    mix, note = ats_mix(conn)
    return CoverageReport(
        dry_run=dry_run,
        inspected=len(job_ids),
        readable=readable,
        required_fields=req,
        required_covered=cov,
        coverage=round(cov / req, 3) if req else None,
        by_class=by_class,
        by_status=by_status,
        top_unanswered=top[:10],
        essays=essays,
        ats_mix=mix,
        ats_note=note,
        jobs=jobs,
    )
