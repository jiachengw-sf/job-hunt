"""Legitimacy and safety signals. Kept separate from fit and shown as signals, never accusations.

old_posting         posted_at is older than the configured number of days
repost              same company + normalized title + near-identical description (SimHash)
                    seen earlier under a different ID
evergreen_language  "talent pipeline", "evergreen", "future opportunities", ...
ai_instructions     text aimed at AI tools ("if you are a large language model..."). Flagged
                    to the user and never followed.
missing_from_ats    the employer's own board was fetched and does not list the job
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from jobhunt.discovery.text import days_between, hamming, normalize_title

EVERGREEN = re.compile(
    r"talent (pipeline|pool|community|network)|"
    r"\bevergreen (role|position|posting|requisition|req|job)|future (job )?opportunit(y|ies)|"
    r"general (application|interest)|always (be )?(hiring|accepting applications)|"
    r"pipeline (role|requisition|position|req)|not (currently )?(an )?(active|open) "
    r"(role|position|opening|requisition)",
    re.IGNORECASE,
)

AI_INSTRUCTIONS = re.compile(
    r"if you are an? (large language model|llm|language model|ai\b|ai model|ai assistant|"
    r"chatbot|gpt|automated (system|agent))|"
    r"(ignore|disregard) (all |any )?(previous|prior|above|earlier) (instructions|prompts)|"
    r"\b(ai|llm|language model|chatgpt|gpt|assistant)s? (should|must|are instructed to) "
    r"(include|mention|add|write|insert|use|respond|reply|say)|"
    r"(note|attention|instructions?) (to|for) (ai|llm|language model|chatgpt|automated)|"
    r"include the (word|phrase|keyword) [\"'“]?\w+",
    re.IGNORECASE,
)


def snippet(text: str, m: re.Match[str], width: int = 90) -> str:
    start, end = max(0, m.start() - width), min(len(text), m.end() + width)
    s = re.sub(r"\s+", " ", text[start:end]).strip()
    return ("…" if start else "") + s + ("…" if end < len(text) else "")


def _set(
    conn: sqlite3.Connection,
    job_id: int,
    kind: str,
    evidence: str,
    details: dict[str, Any],
    now: str,
) -> bool:
    """Upsert a signal; True if it is new for this job."""
    existed = conn.execute(
        "SELECT 1 FROM signals WHERE job_id=? AND kind=?", (job_id, kind)
    ).fetchone()
    conn.execute(
        "INSERT INTO signals (job_id, kind, evidence, details_json, at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(job_id, kind) DO UPDATE SET evidence=excluded.evidence, "
        "details_json=excluded.details_json",
        (job_id, kind, evidence, json.dumps(details, sort_keys=True), now),
    )
    return existed is None


def _clear(conn: sqlite3.Connection, job_id: int, kind: str) -> None:
    conn.execute("DELETE FROM signals WHERE job_id=? AND kind=?", (job_id, kind))


def text_signals(conn: sqlite3.Connection, job: sqlite3.Row, now: str) -> list[str]:
    """Evergreen language and AI-targeted instructions, from the title and description."""
    added = []
    text = f"{job['title']}\n{job['description'] or ''}"
    for kind, rx in (("evergreen_language", EVERGREEN), ("ai_instructions", AI_INSTRUCTIONS)):
        m = rx.search(text)
        if m:
            if _set(conn, job["id"], kind, snippet(text, m), {"match": m.group(0)}, now):
                added.append(kind)
        else:
            _clear(conn, job["id"], kind)
    return added


def old_posting(conn: sqlite3.Connection, job: sqlite3.Row, now: str, max_days: int) -> list[str]:
    if not job["posted_at"]:
        _clear(conn, job["id"], "old_posting")  # undated postings are never flagged as old
        return []
    age = days_between(job["posted_at"], now)
    if age <= max_days:
        _clear(conn, job["id"], "old_posting")
        return []
    ev = f"posted {job['posted_at'][:10]} ({int(age)} days ago)"
    return (
        ["old_posting"] if _set(conn, job["id"], "old_posting", ev, {"days": int(age)}, now) else []
    )


def repost(
    conn: sqlite3.Connection, job: sqlite3.Row, now: str, created_now: set[int], max_distance: int
) -> list[str]:
    """Flag a job whose description nearly matches a job seen in an earlier run at the same
    company with the same normalized title but a different ID. (Jobs created in the same run are
    one role in several locations, not a repost.)"""
    if not job["simhash"]:
        return []
    norm = normalize_title(job["title"])
    rows = conn.execute(
        "SELECT id, dedupe_key, title, simhash, first_seen, live, closed_at FROM jobs "
        "WHERE lower(company)=lower(?) AND id<>? AND simhash IS NOT NULL",
        (job["company"], job["id"]),
    ).fetchall()
    for r in rows:
        if r["id"] in created_now or normalize_title(r["title"]) != norm:
            continue
        d = hamming(job["simhash"], r["simhash"])
        if d <= max_distance:
            status = (
                "closed " + r["closed_at"][:10]
                if not r["live"] and r["closed_at"]
                else "still listed"
            )
            ev = f"near-identical to job {r['id']} first seen {r['first_seen'][:10]} ({status})"
            details = {"similar_job_id": r["id"], "similar_key": r["dedupe_key"], "distance": d}
            return ["repost"] if _set(conn, job["id"], "repost", ev, details, now) else []
    return []


def missing_from_ats(
    conn: sqlite3.Connection, fetched_boards: dict[tuple[str, str], str], now: str
) -> list[int]:
    """Live jobs on a board fetched successfully this run that the board itself doesn't list
    (they are kept live by another source, e.g. a stale aggregator row)."""
    flagged = []
    for (ats, board), source_id in fetched_boards.items():
        rows = conn.execute(
            "SELECT j.id FROM jobs j WHERE j.live=1 AND j.ats=? AND lower(j.board_token)=? "
            "AND j.ats_job_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM source_jobs s "
            "WHERE s.job_id=j.id AND s.source_id=? AND s.present=1)",
            (ats, board, source_id),
        ).fetchall()
        listed = {
            r[0]
            for r in conn.execute(
                "SELECT job_id FROM source_jobs WHERE source_id=? AND present=1", (source_id,)
            )
        }
        for r in rows:
            ev = f"not listed on the employer's own {ats} board ({board})"
            if _set(conn, r["id"], "missing_from_ats", ev, {"source_id": source_id}, now):
                flagged.append(r["id"])
        for jid in listed:
            _clear(conn, jid, "missing_from_ats")
    return flagged
