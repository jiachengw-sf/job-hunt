"""Run the fit stages over the store: gates → recall → requirements → scores → priority → tasks.

Writes gate_results, requirements (code rows), fits, tasks, and a runs row. Re-running is safe:
everything code-derived is recomputed in place. Ingested AI results (requirement rows and
``judgments``) are kept and used instead of the code ones while the posting and profile they were
judged on are unchanged; when either changes they are dropped and a new task is written.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from jobhunt.discovery.resolver import SUPPORTED
from jobhunt.fit import gates as gt
from jobhunt.fit.jobview import JobView, load_jobs
from jobhunt.fit.profile_lines import (
    ProfileLine,
    deidentified_resume,
    profile_lines,
    years_of_experience,
)
from jobhunt.fit.recall import Embedder, recall
from jobhunt.fit.requirements import Requirement, extract, prematch, required_years
from jobhunt.fit.scoring import Qualification, preference, priority, qualify
from jobhunt.fit.tasks import TASK_KIND, basis_hash, write_task
from jobhunt.models import Answers, Config, Preferences, Profile, Watchlist

ELIGIBILITY_GATES = {"sponsorship", "citizenship", "clearance", "graduation", "degree", "language"}
TOP_SHOWN = 15


@dataclass
class Inputs:
    profile: Profile
    prefs: Preferences
    answers: Answers
    watchlist: Watchlist
    cfg: Config
    today: date
    now: str

    @property
    def ctx(self) -> gt.Context:
        return gt.Context(self.prefs, self.answers, self.profile, self.cfg.fit, self.today)

    @property
    def watch_companies(self) -> set[str]:
        return {c.name for c in self.watchlist.companies}


# --- output models --------------------------------------------------------------------------


class GateSummary(BaseModel):
    jobs: int
    passed: int
    unclear: int
    failed: int
    drops_by_gate: dict[str, int] = Field(
        description="Jobs failing each gate (a job can fail several)."
    )
    user_questions: dict[str, int] = Field(
        default_factory=dict, description="Unclear gates only you can resolve, with job counts."
    )


class FitBrief(BaseModel):
    id: int
    company: str
    title: str
    band: str | None
    provisional: bool
    priority: float
    preference: float | None
    relevance_rank: int | None
    flags: list[str] = Field(default_factory=list)


class MatchReport(BaseModel):
    run_id: int | None
    dry_run: bool
    gates: GateSummary
    recall: dict[str, Any]
    bands: dict[str, int]
    provisional: int
    tasks: dict[str, int]
    top: list[FitBrief]
    warnings: list[str] = Field(default_factory=list)


# --- gates ----------------------------------------------------------------------------------


def _upsert_fit(conn: sqlite3.Connection, job_id: int, now: str, **cols: Any) -> None:
    names = ["job_id", "computed_at", *cols]
    vals = [job_id, now, *cols.values()]
    sets = ", ".join(f"{c}=excluded.{c}" for c in ["computed_at", *cols])
    conn.execute(
        f"INSERT INTO fits ({', '.join(names)}) VALUES ({', '.join('?' * len(names))}) "
        f"ON CONFLICT(job_id) DO UPDATE SET {sets}",
        vals,
    )


def _store_gates(
    conn: sqlite3.Connection, job_id: int, results: list[gt.GateResult], now: str
) -> None:
    names = [r.gate for r in results]
    conn.execute(
        f"DELETE FROM gate_results WHERE job_id=? AND gate NOT IN ({','.join('?' * len(names))})",
        (job_id, *names),
    )
    for r in results:
        conn.execute(
            "INSERT INTO gate_results (job_id, gate, result, evidence, reason, at) VALUES "
            "(?,?,?,?,?,?) "
            "ON CONFLICT(job_id, gate) DO UPDATE SET result=excluded.result, "
            "evidence=excluded.evidence, reason=excluded.reason, at=excluded.at",
            (job_id, r.gate, r.result, r.evidence, r.reason, now),
        )
    status = gt.overall(results)
    shown = [r.as_dict() for r in results if r.result in ("fail", "unclear")]
    _upsert_fit(conn, job_id, now, gate_status=status, gate_reasons_json=json.dumps(shown))


def _judged(conn: sqlite3.Connection, job_id: int, res: list[gt.GateResult]) -> list[gt.GateResult]:
    """Apply ingested judgments: an AI-routed unclear gate takes the judged result, and a
    dealbreaker the posting hits becomes a failing gate."""
    rows = conn.execute("SELECT * FROM judgments WHERE job_id=?", (job_id,)).fetchall()
    if not rows:
        return res
    gates = {r["key"]: r for r in rows if r["kind"] == "gate"}
    out = []
    for g in res:
        j = gates.get(g.gate)
        if g.result == "unclear" and g.route == "ai" and j is not None:
            verdict = "passes" if j["result"] == "pass" else "fails"
            g = gt.GateResult(
                g.gate, j["result"], f"judged from the posting: {verdict} ({g.reason})", j["quote"]
            )
        out.append(g)
    for r in rows:
        if r["kind"] == "dealbreaker" and r["result"] == "hit":
            out.append(
                gt.GateResult("dealbreaker", "fail", f"your dealbreaker: {r['key']}", r["quote"])
            )
    return out


def drop_stale_judgments(conn: sqlite3.Connection, job: JobView, lines: list[ProfileLine]) -> bool:
    """Drop a job's AI rows when its posting or the profile changed since they were judged.
    Returns True when the job holds an ingested result that is still current."""
    row = conn.execute(
        "SELECT basis_hash FROM tasks WHERE job_id=? AND kind=? AND status='ingested' "
        "ORDER BY ingested_at DESC LIMIT 1",
        (job.id, TASK_KIND),
    ).fetchone()
    if row is None:
        return False
    if row["basis_hash"] == basis_hash(job, lines):
        return True
    conn.execute("DELETE FROM requirements WHERE job_id=? AND source='ai'", (job.id,))
    conn.execute("DELETE FROM judgments WHERE job_id=?", (job.id,))
    conn.execute(
        "UPDATE tasks SET status='expired' WHERE job_id=? AND kind=? AND status='ingested'",
        (job.id, TASK_KIND),
    )
    conn.execute("UPDATE fits SET narrative_json=NULL, disagreement=NULL WHERE job_id=?", (job.id,))
    return False


def run_gate_stage(
    conn: sqlite3.Connection, inp: Inputs, jobs: list[JobView]
) -> tuple[GateSummary, dict[int, list[gt.GateResult]]]:
    ctx = inp.ctx
    lines = profile_lines(inp.profile)
    by_job: dict[int, list[gt.GateResult]] = {}
    drops: Counter[str] = Counter()
    questions: Counter[str] = Counter()
    status_n: Counter[str] = Counter()
    for job in jobs:
        drop_stale_judgments(conn, job, lines)
        res = _judged(conn, job.id, gt.run_gates(job, ctx))
        by_job[job.id] = res
        _store_gates(conn, job.id, res, inp.now)
        status_n[gt.overall(res)] += 1
        for r in res:
            if r.result == "fail":
                drops[r.gate] += 1
            elif r.result == "unclear" and r.route == "user":
                questions[f"{r.gate}: {r.reason}"] += 1
    return (
        GateSummary(
            jobs=len(jobs),
            passed=status_n["passed"],
            unclear=status_n["unclear"],
            failed=status_n["failed"],
            drops_by_gate=dict(drops.most_common()),
            user_questions=dict(questions.most_common()),
        ),
        by_job,
    )


# --- requirements ---------------------------------------------------------------------------


def _ai_requirements(conn: sqlite3.Connection, job_id: int) -> list[Requirement]:
    rows = conn.execute(
        "SELECT * FROM requirements WHERE job_id=? AND source='ai' ORDER BY seq", (job_id,)
    ).fetchall()
    return [
        Requirement(
            seq=r["seq"],
            text=r["text"],
            importance=r["importance"],
            type=r["type"],
            section=r["section"] or "",
            status=r["status"],
            tier=r["tier"],
            evidence=r["evidence"],
            evidence_ref=r["evidence_ref"],
        )
        for r in rows
    ]


def _apply_gate_evidence(
    reqs: list[Requirement], results: list[gt.GateResult], lines: list[ProfileLine]
) -> None:
    """An eligibility/degree requirement whose text a passing gate matched is met (stated)."""
    edu_line = next((ln for ln in lines if ln.section == "education"), None)
    for r in reqs:
        if r.status or r.type not in ("eligibility", "degree") or r.skills:
            continue
        key = re.sub(r"\s+", " ", r.text.lower())[:60]
        for g in results:
            if g.gate not in ELIGIBILITY_GATES or g.result != "pass" or not g.evidence:
                continue
            ev = re.sub(r"\s+", " ", g.evidence.lower())
            if key[:40] in ev or ev.strip("…")[:40] in r.text.lower():
                r.status, r.tier = "met", "stated"
                if g.gate == "degree" and edu_line:
                    r.evidence, r.evidence_ref = edu_line.text, edu_line.ref
                else:  # backed by the user's own answers, which never go to the model
                    r.evidence, r.evidence_ref = (
                        "(from your answers; filled by code)",
                        f"answers.{g.gate}",
                    )
                break


def _store_requirements(conn: sqlite3.Connection, job_id: int, reqs: list[Requirement]) -> None:
    conn.execute("DELETE FROM requirements WHERE job_id=? AND source='code'", (job_id,))
    for r in reqs:
        conn.execute(
            "INSERT INTO requirements (job_id, seq, text, importance, type, status, tier, "
            "evidence, "
            "evidence_ref, section, source) VALUES (?,?,?,?,?,?,?,?,?,?,'code')",
            (
                job_id,
                r.seq,
                r.text,
                r.importance,
                r.type,
                r.status,
                r.tier,
                r.evidence,
                r.evidence_ref,
                r.section,
            ),
        )


# --- volume guard ---------------------------------------------------------------------------


def _norm_company(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", "", name.lower())
    return re.sub(
        r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|labs?)\b", "", s
    ).strip()


def recent_applications(conn: sqlite3.Connection, today: date, window_days: int) -> Counter[str]:
    since = (today - timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        "SELECT j.company FROM applications a JOIN jobs j ON j.id=a.job_id "
        "WHERE a.applied_on IS NOT NULL AND a.applied_on >= ?",
        (since,),
    ).fetchall()
    return Counter(_norm_company(r["company"]) for r in rows)


# --- match ----------------------------------------------------------------------------------


@dataclass
class _Scored:
    job: JobView
    qual: Qualification
    pref: float | None
    prio: float
    flags: list[str] = field(default_factory=list)


def run_match(
    conn: sqlite3.Connection,
    inp: Inputs,
    *,
    tasks_dir: Path,
    results_dir: Path,
    embedder: Embedder | None,
    embed_note: str | None,
    dry_run: bool,
) -> MatchReport:
    from jobhunt.discovery.pipeline import finish_run, start_run

    cfg = inp.cfg.fit
    run_id = start_run(conn, "match", inp.now)
    jobs = load_jobs(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        gate_summary, gate_results = run_gate_stage(conn, inp, jobs)
        eligible = [j for j in jobs if gt.overall(gate_results[j.id]) != "failed"]
        lines = profile_lines(inp.profile)
        rec = recall(
            conn,
            eligible,
            deidentified_resume(inp.profile),
            inp.prefs,
            cfg,
            watch_companies=inp.watch_companies,
            embedder=embedder,
        )
        your_years = years_of_experience(inp.profile, inp.answers, inp.today)
        applied = recent_applications(conn, inp.today, inp.cfg.volume_guard.window_days)
        referrals = {
            r["job_id"]
            for r in conn.execute(
                "SELECT job_id FROM applications WHERE coalesce(trim(referral_contact),'') != ''"
            )
        }
        watch = {c.lower() for c in inp.watch_companies}
        scored: list[_Scored] = []
        reqs_by_job: dict[int, tuple[list[Requirement], bool]] = {}
        for job in eligible:
            r = rec.by_job.get(job.id)
            reqs = _ai_requirements(conn, job.id)
            messy = False
            if not reqs:
                ex = extract(job.description)
                reqs, messy = ex.requirements, ex.messy or not ex.requirements
                prematch(reqs, lines)
                _apply_gate_evidence(reqs, gate_results[job.id], lines)
                _store_requirements(conn, job.id, reqs)
            reqs_by_job[job.id] = (reqs, messy)
            need, _ = required_years(job.description)
            q = qualify(reqs, gate_results[job.id], cfg, required_years=need, your_years=your_years)
            p = preference(job, inp.prefs, cfg)
            rel = max(0.0, 1 - (r.rank - 1) / cfg.recall_top_k) if (r and r.recalled) else 0.0
            unclear_degree = any(
                g.gate == "degree" and g.result == "unclear" for g in gate_results[job.id]
            )
            pr, pflags = priority(
                job,
                q.band,
                p.score,
                cfg,
                inp.today,
                referral=job.id in referrals,
                watchlist=job.company.lower() in watch,
                relevance=rel,
                degree_above=gt.degree_above(job, inp.ctx) if unclear_degree else None,
            )
            flags = p.flags + pflags
            n_apps = applied.get(_norm_company(job.company), 0)
            if n_apps >= inp.cfg.volume_guard.max_applications_per_company:
                flags.append(
                    f"volume guard: {n_apps} applications to {job.company} in the last "
                    f"{inp.cfg.volume_guard.window_days} days; another may be auto-rejected"
                )
            _upsert_fit(
                conn,
                job.id,
                inp.now,
                relevance=r.score if r else None,
                recall_rank=r.rank if r else None,
                recalled=1 if (r and r.recalled) else 0,
                band=q.band,
                provisional=1 if q.provisional else 0,
                preference_score=p.score,
                priority=pr.score,
                qualification_json=json.dumps(q.as_dict()),
                preference_json=json.dumps(p.components),
                priority_json=json.dumps(pr.components),
                flags_json=json.dumps(flags),
            )
            scored.append(_Scored(job, q, p.score, pr.score, flags))
        for job in jobs:  # gated-out jobs: no score, priority 0
            if gt.overall(gate_results[job.id]) == "failed":
                _upsert_fit(
                    conn,
                    job.id,
                    inp.now,
                    relevance=None,
                    recall_rank=None,
                    recalled=0,
                    band=None,
                    provisional=0,
                    preference_score=None,
                    priority=0.0,
                    qualification_json=None,
                    preference_json=None,
                    priority_json=None,
                    flags_json="[]",
                )

        task_counts: Counter[str] = Counter()
        no_text = 0
        for s in scored:
            r = rec.by_job.get(s.job.id)
            if not (r and r.recalled):
                continue
            has_text = bool(s.job.description.strip())
            if not has_text and r.rank <= cfg.describe_top_k and s.job.ats in SUPPORTED:
                no_text += 1  # `run` can read these from the ATS
            # the top recalled jobs are judged even below the threshold: an unjudged band holds
            # their priority down, so the threshold alone would never let them be judged
            if s.prio < cfg.priority_threshold and not (has_text and r.rank <= cfg.judge_top_k):
                continue
            if drop_stale_judgments(conn, s.job, lines):  # already judged on this posting
                continue
            reqs, messy = reqs_by_job[s.job.id]
            unclear_ai = [
                g for g in gate_results[s.job.id] if g.result == "unclear" and g.route == "ai"
            ]
            outcome = write_task(
                conn,
                tasks_dir,
                results_dir,
                s.job,
                reqs,
                lines,
                messy=messy,
                unclear_ai=unclear_ai,
                dealbreakers=list(inp.prefs.dealbreakers),
                band=s.qual.band,
                now=inp.now,
                dry_run=dry_run,
            )
            task_counts[outcome] += 1
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    bands = Counter(s.qual.band or "unknown" for s in scored)
    top = sorted(scored, key=lambda s: (-s.prio, s.job.id))[:TOP_SHOWN]
    warnings = sorted({f for s in scored for f in s.flags if f.startswith("volume guard")})
    if rec.note:
        warnings.append(rec.note)
    pending = conn.execute("SELECT count(*) FROM tasks WHERE status='pending'").fetchone()[0]
    recall_info = {
        "mode": rec.mode,
        "ranked": len(rec.by_job),
        "kept": sum(1 for r in rec.by_job.values() if r.recalled),
        "top_k": cfg.recall_top_k,
        "note": embed_note,
    }
    stats = {
        "gates": gate_summary.model_dump(),
        "bands": dict(bands),
        "recall": recall_info,
        "tasks": dict(task_counts),
    }
    finish_run(conn, run_id, "ok", stats, inp.now)
    update_gated_out(conn)
    return MatchReport(
        run_id=None if dry_run else run_id,
        dry_run=dry_run,
        gates=gate_summary,
        recall=recall_info,
        bands=dict(bands),
        provisional=sum(1 for s in scored if s.qual.provisional),
        tasks={
            "written": task_counts["written"],
            "replaced": task_counts["replaced"],
            "unchanged": task_counts["unchanged"],
            "pending_total": pending,
            "no_posting_text": no_text,
        },
        top=[
            FitBrief(
                id=s.job.id,
                company=s.job.company,
                title=s.job.title,
                band=s.qual.band,
                provisional=s.qual.provisional,
                priority=s.prio,
                preference=s.pref,
                relevance_rank=rec.by_job[s.job.id].rank if s.job.id in rec.by_job else None,
                flags=s.flags,
            )
            for s in top
        ],
        warnings=warnings,
    )


def update_gated_out(conn: sqlite3.Connection) -> None:
    """Fill source_yields.gated_out for every fetch run: jobs that run reported as new (or
    baseline) per source and that now fail a gate."""
    conn.execute(
        "UPDATE source_yields SET gated_out = ("
        " SELECT count(DISTINCT e.job_id) FROM events e JOIN fits f ON f.job_id=e.job_id"
        " WHERE e.run_id=source_yields.run_id AND e.source_id=source_yields.source_id"
        " AND e.kind='appeared' AND f.gate_status='failed')"
        " WHERE run_id IN (SELECT id FROM runs WHERE command='fetch')"
    )
