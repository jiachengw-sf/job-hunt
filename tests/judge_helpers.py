"""A deterministic stand-in for the jobhunt-judge skill: builds a result that ingest accepts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def posting_lines(task: dict[str, Any]) -> list[str]:
    body = re.sub(r"</?untrusted_content[^>]*>", "", task["posting"])
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def fake_result(task: dict[str, Any]) -> dict[str, Any]:
    lines = {ln["ref"]: ln["text"] for ln in task["profile_lines"]}
    reqs = []
    for r in task["requirements"]:
        pm = r.get("prematch") or {}
        if pm.get("tier") == "stated" and pm.get("evidence_ref") in lines:
            ref = pm["evidence_ref"]
            reqs.append(
                {
                    "id": r["id"],
                    "text": r["text"],
                    "status": pm["status"],
                    "tier": "stated",
                    "evidence_ref": ref,
                    "evidence_quote": lines[ref],
                }
            )
        elif pm.get("tier") == "stated":  # code-backed from the user's answers
            reqs.append(
                {
                    "id": r["id"],
                    "text": r["text"],
                    "status": pm["status"],
                    "tier": "stated",
                    "evidence_ref": pm["evidence_ref"],
                }
            )
        else:
            reqs.append(
                {
                    "id": r["id"],
                    "text": r["text"],
                    "status": "partial",
                    "tier": "inferred",
                    "note": "related work, not named directly",
                }
            )
    if not reqs:
        text = posting_lines(task)[2] if len(posting_lines(task)) > 2 else task["job"]["title"]
        reqs.append(
            {
                "id": None,
                "text": text,
                "importance": "preferred",
                "type": "other",
                "status": "partial",
                "tier": "inferred",
                "note": "close to past work",
            }
        )
    first_ref = task["profile_lines"][0]["ref"]
    return {
        "task_id": task["task_id"],
        "job_id": task["job"]["id"],
        "requirements": reqs,
        "gates": [
            {
                "gate": g["gate"],
                "result": "pass",
                "quote": (g.get("evidence") or task["job"]["title"]).strip("…"),
            }
            for g in task["unclear_gates"]
        ],
        "dealbreakers": [{"dealbreaker": d, "hit": False} for d in task["dealbreakers"]],
        "narrative": {
            "reasons": [
                {
                    "text": "Relevant background",
                    "evidence_ref": first_ref,
                    "evidence_quote": lines[first_ref],
                }
            ],
            "gaps": [],
            "pitch": "Background lines up with the role.",
            "why_wrong": "The posting is short.",
        },
    }


def judge_all(home: Path, edit=None) -> list[str]:
    """Write a fake result for every pending task file; ``edit(task, result)`` may alter it."""
    written = []
    for f in sorted((home / "work" / "tasks").glob("req-*.json")):
        task = json.loads(f.read_text())
        res = fake_result(task)
        if edit:
            edit(task, res)
        Path(task["result_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(task["result_path"]).write_text(json.dumps(res), encoding="utf-8")
        written.append(task["task_id"])
    return written
