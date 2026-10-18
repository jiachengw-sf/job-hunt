"""Ingest of essay drafts and the one review pass.

Drafts: every sentence's citation is checked (``drafts.grounding``), hidden characters are
stripped, and the joined text must fit the form's limit. Reviews: each ``old`` must occur exactly
once, no ``new`` may add a claim the draft's sources don't back, and only one round is allowed.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import ValidationError

from jobhunt.drafts.essays import (
    DRAFT_KIND,
    REVIEW_KIND,
    EssayDraftResult,
    EssayReviewResult,
    draft_file,
    essay_prompts,
    read_user_text,
    write_draft_file,
)
from jobhunt.drafts.grounding import (
    Sentence,
    Sources,
    check_sentences,
    edit_adds_claims,
    job_sources,
    join_sentences,
    source_text,
)
from jobhunt.fit.jobview import JobView, load_jobs
from jobhunt.ingest.core import Ctx, register, schema_problems
from jobhunt.ingest.verify import Problem, clean_text


def _job(conn: sqlite3.Connection, job_id: int) -> JobView | None:
    jobs = load_jobs(conn, [job_id], live_only=False)
    return jobs[0] if jobs else None


def sources_for(conn: sqlite3.Connection, job: JobView, ctx: Ctx) -> Sources:
    inp = ctx.inputs
    return job_sources(conn, job.text, job.company, inp.profile, inp.answers)


def _limit(text: str, max_length: int | None, path: str, problems: list[Problem]) -> None:
    if max_length and len(text) > max_length:
        problems.append(
            Problem(path, f"{len(text)} characters; the form allows {max_length}. Shorten it.")
        )


class EssayDraftHandler:
    kind = DRAFT_KIND

    def check(
        self, conn: sqlite3.Connection, raw: dict[str, Any], task: sqlite3.Row, ctx: Ctx
    ) -> tuple[Any, list[Problem]]:
        try:
            res = EssayDraftResult.model_validate(raw)
        except ValidationError as e:
            return None, schema_problems(e)
        job = _job(conn, task["job_id"])
        if job is None:
            return None, [Problem("$.job_id", "job not in the store")]
        prompts = {p.field_id: p for p in essay_prompts(conn, job.id)}
        sources = sources_for(conn, job, ctx)
        approved = {
            r["key"]
            for r in conn.execute(
                "SELECT key FROM drafts WHERE job_id=? AND kind='essay' AND status='approved'",
                (job.id,),
            )
        }
        problems: list[Problem] = []
        seen: set[str] = set()
        texts: dict[str, str] = {}
        for i, e in enumerate(res.essays):
            path = f"$.essays[{i}]"
            if e.field_id not in prompts:
                problems.append(Problem(f"{path}.field_id", f"{e.field_id!r} is not a prompt here"))
                continue
            if e.field_id in seen:
                problems.append(Problem(f"{path}.field_id", "appears twice"))
            if e.field_id in approved:
                problems.append(Problem(f"{path}.field_id", "you already approved this answer"))
            seen.add(e.field_id)
            check_sentences(e.sentences, sources, f"{path}.sentences", problems)
            text = join_sentences(e.sentences)
            _limit(text, prompts[e.field_id].max_length, f"{path}.sentences", problems)
            texts[e.field_id] = text
        for fid, p in prompts.items():
            if p.required and fid not in seen and fid not in approved:
                problems.append(Problem("$.essays", f"draft the required prompt {fid!r}"))
        return (res, texts), problems

    def apply(
        self, conn: sqlite3.Connection, parsed: Any, task: sqlite3.Row, ctx: Ctx
    ) -> dict[str, Any]:
        res, texts = parsed
        prompts = {p.field_id: p for p in essay_prompts(conn, task["job_id"])}
        files = []
        for e in res.essays:
            text = texts[e.field_id]
            prov = json.dumps([s.model_dump() for s in e.sentences])
            p = prompts[e.field_id]
            conn.execute(
                "INSERT INTO drafts (job_id, kind, key, prompt, max_length, status, original, "
                "text, provenance_json, task_id, created_at, updated_at) "
                "VALUES (?, 'essay', ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(job_id, kind, key) DO UPDATE SET prompt=excluded.prompt, "
                "max_length=excluded.max_length, status='draft', original=excluded.original, "
                "text=excluded.text, provenance_json=excluded.provenance_json, review_json=NULL, "
                "approved_text=NULL, edit_ratio=NULL, task_id=excluded.task_id, "
                "updated_at=excluded.updated_at",
                (task["job_id"], e.field_id, p.label, p.max_length, text, text, prov,
                 task["id"], ctx.now, ctx.now),
            )  # fmt: skip
            if not ctx.dry_run:
                files.append(str(write_draft_file(ctx.paths.work, task["job_id"], p, e.field_id,
                                                  text)))  # fmt: skip
        return {"essays": len(res.essays), "files": files}


class EssayReviewHandler:
    kind = REVIEW_KIND

    def check(
        self, conn: sqlite3.Connection, raw: dict[str, Any], task: sqlite3.Row, ctx: Ctx
    ) -> tuple[Any, list[Problem]]:
        try:
            res = EssayReviewResult.model_validate(raw)
        except ValidationError as e:
            return None, schema_problems(e)
        job = _job(conn, task["job_id"])
        if job is None:
            return None, [Problem("$.job_id", "job not in the store")]
        rows = {
            r["key"]: r
            for r in conn.execute(
                "SELECT * FROM drafts WHERE job_id=? AND kind='essay' AND status IN "
                "('draft', 'reviewed')",
                (job.id,),
            )
        }
        sources = sources_for(conn, job, ctx)
        problems: list[Problem] = []
        current = {k: r["text"] for k, r in rows.items()}
        for k, r in rows.items():
            if r["status"] == "reviewed":
                problems.append(Problem("$", f"{k!r} was already reviewed: one round only"))
        for i, e in enumerate(res.edits):
            path = f"$.edits[{i}]"
            if e.field_id not in rows:
                problems.append(Problem(f"{path}.field_id", f"no draft {e.field_id!r} to review"))
                continue
            text = current[e.field_id]
            n = text.count(e.old)
            if n != 1:
                problems.append(
                    Problem(
                        f"{path}.old",
                        "not found in the draft"
                        if n == 0
                        else f"appears {n} times; make it unique",
                    )
                )
                continue
            sentences = [
                Sentence.model_validate(s) for s in json.loads(rows[e.field_id]["provenance_json"])
            ]
            cited = "\n".join(source_text(s, sources) or "" for s in sentences)
            added = edit_adds_claims(e.old, e.new, cited, sources.posting)
            if added:
                problems.append(
                    Problem(f"{path}.new", f"adds {'; '.join(added)} the sources don't back")
                )
                continue
            current[e.field_id] = text.replace(e.old, clean_text(e.new), 1)
        for k, text in current.items():
            current[k] = clean_text(text).strip()
            _limit(current[k], rows[k]["max_length"], f"$.edits ({k})", problems)
        return (res, current), problems

    def apply(
        self, conn: sqlite3.Connection, parsed: Any, task: sqlite3.Row, ctx: Ctx
    ) -> dict[str, Any]:
        res, current = parsed
        prompts = {p.field_id: p for p in essay_prompts(conn, task["job_id"])}
        kept_user_edits = []
        for key, text in current.items():
            row = conn.execute(
                "SELECT text FROM drafts WHERE job_id=? AND kind='essay' AND key=?",
                (task["job_id"], key),
            ).fetchone()
            edits = [e.model_dump() for e in res.edits if e.field_id == key]
            conn.execute(
                "UPDATE drafts SET text=?, review_json=?, status='reviewed', updated_at=? "
                "WHERE job_id=? AND kind='essay' AND key=?",
                (text, json.dumps(edits), ctx.now, task["job_id"], key),
            )
            if ctx.dry_run:
                continue
            f = draft_file(ctx.paths.work, task["job_id"], key)
            if f.exists() and read_user_text(f) != row["text"]:  # the user already edited it
                alt = write_draft_file(
                    ctx.paths.work, task["job_id"], prompts.get(key), key, text, suffix=".reviewed"
                )
                kept_user_edits.append(str(alt))
            else:
                write_draft_file(ctx.paths.work, task["job_id"], prompts.get(key), key, text)
        return {
            "edits": len(res.edits),
            "reviewed": sorted(current),
            "kept_your_edits": kept_user_edits,
        }


register(EssayDraftHandler())
register(EssayReviewHandler())
