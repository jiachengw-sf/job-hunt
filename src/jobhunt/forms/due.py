"""Which jobs' application forms are due a read: the one rule `run` and the fit hints share."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta

from jobhunt.discovery.text import iso


def form_read_due(
    conn: sqlite3.Connection, job_ids: Iterable[int], *, now: str, retry_days: int
) -> set[int]:
    """The jobs among ``job_ids`` whose form should be read: never read, or the last read
    failed and since then the posting changed or reappeared, or the failure is ``retry_days``
    old. A form read OK (or unsupported) is never due here, text or not."""
    ids = list(dict.fromkeys(job_ids))
    if not ids:
        return set()
    cutoff = iso(datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(days=retry_days))
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT j.id FROM jobs j LEFT JOIN form_specs s ON s.job_id=j.id WHERE j.id IN ({marks}) "
        "AND (s.job_id IS NULL OR (s.status NOT IN ('ok','unsupported') AND (s.fetched_at <= ? "
        "OR EXISTS (SELECT 1 FROM events e WHERE e.job_id=j.id "
        "AND e.kind IN ('changed','reappeared') AND e.at > s.fetched_at))))",
        (*ids, cutoff),
    ).fetchall()
    return {r[0] for r in rows}
