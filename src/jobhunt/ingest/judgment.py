"""Ingest of ``requirement_evidence`` results (the jobhunt-judge skill's output).

Checked against code truth, not the task file: the posting text comes from the store, the profile
lines from the profile, the unclear gates from re-running the gates, the dealbreakers from the
preferences. Accepted results replace the job's requirement rows with AI rows and record the
resolved gates and dealbreakers; ``jobhunt ingest`` then recomputes the band.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import ValidationError

from jobhunt.fit import gates as gt
from jobhunt.fit.jobview import JobView, load_jobs
from jobhunt.fit.profile_lines import ProfileLine, profile_lines
from jobhunt.fit.requirements import extract, find_skills
from jobhunt.fit.tasks import TASK_KIND, RequirementEvidenceResult, ReqVerdict
from jobhunt.ingest.core import Ctx, register, schema_problems
from jobhunt.ingest.verify import Problem, check_line, clean_text, quote_in

SKILL_TYPES = {"skill", "experience"}


def _posting(conn: sqlite3.Connection, job_id: int) -> JobView | None:
    jobs = load_jobs(conn, [job_id], live_only=False)
    return jobs[0] if jobs else None


def _check_requirement(
    i: int,
    r: ReqVerdict,
    code: dict[int, sqlite3.Row],
    job: JobView,
    lines: dict[str, ProfileLine],
    messy: bool,
    problems: list[Problem],
) -> None:
    path = f"$.requirements[{i}]"
    row = code.get(r.id) if r.id is not None else None
    if r.id is not None and row is None:
        problems.append(Problem(f"{path}.id", f"no requirement {r.id} in the task"))
        return
    if r.id is None:
        if not messy:
            problems.append(
                Problem(f"{path}.id", "extract_requirements is false: use the task's ids")
            )
        if r.importance is None or r.type is None:
            problems.append(Problem(path, "a new requirement needs importance and type"))
    if not quote_in(r.text, job.text):
        problems.append(Problem(f"{path}.text", "not found in the posting; quote it exactly"))
    if r.tier == "none" and r.status != "unmet":
        problems.append(Problem(f"{path}.status", "tier none means no evidence: status unmet"))
    if r.tier == "stated" and r.status == "unmet":
        problems.append(Problem(f"{path}.tier", "a stated line means met or partial, not unmet"))
    if r.tier == "inferred" and not (r.note or "").strip():
        problems.append(Problem(f"{path}.note", "explain an inferred match in note"))
    if r.tier == "stated":
        if r.evidence_ref and r.evidence_ref.startswith("answers.") and row is not None:
            # code backed this from the user's own answers; the model never sees them
            if row["evidence_ref"] != r.evidence_ref:
                problems.append(Problem(f"{path}.evidence_ref", "answers.* refs are code-only"))
            return
        line = check_line(r.evidence_ref, r.evidence_quote, lines, path, problems)
        if line is None:
            return
        rtype = r.type or (row["type"] if row is not None else None)
        want = find_skills(r.text)
        if rtype in SKILL_TYPES and want and not set(want) & set(find_skills(line.text)):
            problems.append(
                Problem(
                    f"{path}.tier",
                    f"profile line {line.ref} doesn't name {', '.join(want)}; "
                    "stated needs the line to name the skill, otherwise use inferred",
                )
            )
    elif r.evidence_ref or r.evidence_quote:
        check_line(r.evidence_ref, r.evidence_quote, lines, path, problems)


class RequirementEvidence:
    kind = TASK_KIND

    def check(
        self, conn: sqlite3.Connection, raw: dict[str, Any], task: sqlite3.Row, ctx: Ctx
    ) -> tuple[RequirementEvidenceResult | None, list[Problem]]:
        try:
            res = RequirementEvidenceResult.model_validate(raw)
        except ValidationError as e:
            return None, schema_problems(e)
        problems: list[Problem] = []
        job = _posting(conn, task["job_id"])
        if job is None:
            return None, [Problem("$.job_id", f"job {task['job_id']} is not in the store")]
        inp = ctx.inputs
        lines = {ln.ref: ln for ln in profile_lines(inp.profile)}
        code = {
            r["seq"]: r
            for r in conn.execute(
                "SELECT * FROM requirements WHERE job_id=? AND source='code'", (job.id,)
            )
        }
        ex = extract(job.description)
        messy = ex.messy or not ex.requirements
        seen: set[int] = set()
        for i, r in enumerate(res.requirements):
            if r.id is not None:
                if r.id in seen:
                    problems.append(Problem(f"$.requirements[{i}].id", f"{r.id} appears twice"))
                seen.add(r.id)
            _check_requirement(i, r, code, job, lines, messy, problems)
        missing = sorted(set(code) - seen)
        if missing and not messy:
            problems.append(
                Problem("$.requirements", f"requirements {missing} have no verdict; judge each")
            )
        if not res.requirements:
            problems.append(Problem("$.requirements", "judge at least one requirement"))

        unclear = {
            g.gate: g
            for g in gt.run_gates(job, inp.ctx)
            if g.result == "unclear" and g.route == "ai"
        }
        got: set[str] = set()
        for i, g in enumerate(res.gates):
            path = f"$.gates[{i}]"
            if g.gate not in unclear:
                problems.append(Problem(f"{path}.gate", f"{g.gate!r} is not an unclear gate here"))
            elif g.gate in got:
                problems.append(Problem(f"{path}.gate", f"{g.gate!r} appears twice"))
            got.add(g.gate)
            if not quote_in(g.quote, job.text):
                problems.append(Problem(f"{path}.quote", "not found in the posting"))
        for name in sorted(set(unclear) - got):
            problems.append(Problem("$.gates", f"resolve unclear gate {name!r} (pass|fail)"))

        wanted = list(inp.prefs.dealbreakers)
        given = {d.dealbreaker for d in res.dealbreakers}
        for i, d in enumerate(res.dealbreakers):
            path = f"$.dealbreakers[{i}]"
            if d.dealbreaker not in wanted:
                problems.append(
                    Problem(f"{path}.dealbreaker", "not one of the user's dealbreakers")
                )
            if d.hit and not (d.quote and quote_in(d.quote, job.text)):
                problems.append(Problem(f"{path}.quote", "a hit needs an exact posting quote"))
        for name in wanted:
            if name not in given:
                problems.append(Problem("$.dealbreakers", f"check dealbreaker {name!r}"))

        for i, reason in enumerate(res.narrative.reasons):
            check_line(
                reason.evidence_ref,
                reason.evidence_quote,
                lines,
                f"$.narrative.reasons[{i}]",
                problems,
            )
        return res, problems

    def apply(
        self,
        conn: sqlite3.Connection,
        res: RequirementEvidenceResult,
        task: sqlite3.Row,
        ctx: Ctx,
    ) -> dict[str, Any]:
        job_id = task["job_id"]
        lines = {ln.ref: ln for ln in profile_lines(ctx.inputs.profile)}
        code = {
            r["seq"]: r
            for r in conn.execute(
                "SELECT * FROM requirements WHERE job_id=? AND source='code'", (job_id,)
            )
        }
        conn.execute("DELETE FROM requirements WHERE job_id=? AND source='ai'", (job_id,))
        next_seq = max(code, default=-1) + 1
        for r in res.requirements:
            row = code.get(r.id) if r.id is not None else None
            if row is None:
                seq, next_seq = next_seq, next_seq + 1
            else:
                seq = r.id
            line = lines.get(r.evidence_ref or "")
            if r.tier == "stated" and line is None and row is not None:
                evidence = row["evidence"]  # code-backed answers.* line
            else:
                evidence = line.text if (line and r.tier == "stated") else None
            conn.execute(
                "INSERT INTO requirements (job_id, seq, text, importance, type, status, tier, "
                "evidence, evidence_ref, section, note, source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,'ai')",
                (
                    job_id,
                    seq,
                    clean_text(r.text),
                    r.importance or (row["importance"] if row else "preferred"),
                    r.type or (row["type"] if row else "other"),
                    r.status,
                    r.tier,
                    evidence,
                    r.evidence_ref if r.tier != "none" else None,
                    row["section"] if row else None,
                    clean_text(r.note) if r.note else None,
                ),
            )
        conn.execute("DELETE FROM judgments WHERE job_id=?", (job_id,))
        for g in res.gates:
            conn.execute(
                "INSERT INTO judgments (job_id, kind, key, result, quote, task_id, at) "
                "VALUES (?, 'gate', ?, ?, ?, ?, ?)",
                (job_id, g.gate, g.result, clean_text(g.quote), task["id"], ctx.now),
            )
        for d in res.dealbreakers:
            conn.execute(
                "INSERT INTO judgments (job_id, kind, key, result, quote, task_id, at) "
                "VALUES (?, 'dealbreaker', ?, ?, ?, ?, ?)",
                (
                    job_id,
                    d.dealbreaker,
                    "hit" if d.hit else "clear",
                    clean_text(d.quote) if d.quote else None,
                    task["id"],
                    ctx.now,
                ),
            )
        n = res.narrative
        narrative = {
            "reasons": [
                {
                    "text": clean_text(r.text),
                    "evidence_ref": r.evidence_ref,
                    "evidence_quote": lines[r.evidence_ref].text,
                }
                for r in n.reasons
            ],
            "gaps": [clean_text(g) for g in n.gaps],
            "pitch": clean_text(n.pitch),
            "why_wrong": clean_text(n.why_wrong),
            "task_id": task["id"],
        }
        dis = (
            json.dumps(
                {
                    "band": res.band_disagreement.band,
                    "reason": clean_text(res.band_disagreement.reason),
                }
            )
            if res.band_disagreement
            else None
        )
        conn.execute(
            "UPDATE fits SET narrative_json=?, disagreement=? WHERE job_id=?",
            (json.dumps(narrative), dis, job_id),
        )
        return {
            "requirements": len(res.requirements),
            "gates": {g.gate: g.result for g in res.gates},
            "dealbreakers_hit": [d.dealbreaker for d in res.dealbreakers if d.hit],
        }


register(RequirementEvidence())
