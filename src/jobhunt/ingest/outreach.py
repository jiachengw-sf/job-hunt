"""Ingest of outreach drafts: grounded sentences, a short limit, no hidden characters."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import ValidationError

from jobhunt.drafts.grounding import check_sentences, join_sentences
from jobhunt.drafts.outreach import TASK_KIND, OutreachResult, draft_key, write_outreach_file
from jobhunt.ingest.core import Ctx, register, schema_problems
from jobhunt.ingest.essays import _job, sources_for
from jobhunt.ingest.verify import Problem, clean_text


class OutreachHandler:
    kind = TASK_KIND

    def check(
        self, conn: sqlite3.Connection, raw: dict[str, Any], task: sqlite3.Row, ctx: Ctx
    ) -> tuple[Any, list[Problem]]:
        try:
            res = OutreachResult.model_validate(raw)
        except ValidationError as e:
            return None, schema_problems(e)
        job = _job(conn, task["job_id"])
        if job is None:
            return None, [Problem("$.job_id", "job not in the store")]
        problems: list[Problem] = []
        if res.kind != task["subject"]:
            problems.append(Problem("$.kind", f"must be {task['subject']!r}"))
        check_sentences(res.sentences, sources_for(conn, job, ctx), "$.sentences", problems)
        text = join_sentences(res.sentences)
        limit = ctx.inputs.cfg.drafts.outreach_max_chars
        if len(text) > limit:
            problems.append(
                Problem("$.sentences", f"{len(text)} characters; keep it under {limit}")
            )
        return (res, text), problems

    def apply(
        self, conn: sqlite3.Connection, parsed: Any, task: sqlite3.Row, ctx: Ctx
    ) -> dict[str, Any]:
        res, text = parsed
        job_id = task["job_id"]
        key = draft_key(conn, job_id, res.kind)
        subject = clean_text(res.subject) if res.subject else None
        conn.execute(
            "INSERT INTO drafts (job_id, kind, key, prompt, max_length, status, original, text, "
            "provenance_json, task_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_id, kind, key) DO UPDATE SET prompt=excluded.prompt, status='draft', "
            "original=excluded.original, text=excluded.text, "
            "provenance_json=excluded.provenance_json, task_id=excluded.task_id, "
            "updated_at=excluded.updated_at",
            (job_id, res.kind, key, subject, ctx.inputs.cfg.drafts.outreach_max_chars, text, text,
             json.dumps([s.model_dump() for s in res.sentences]), task["id"], ctx.now, ctx.now),
        )  # fmt: skip
        f = None
        if not ctx.dry_run:
            f = str(write_outreach_file(ctx.paths.work, job_id, res.kind, key, subject, text))
        return {"kind": res.kind, "key": key, "chars": len(text), "file": f}


register(OutreachHandler())
