"""A job as the fit engine sees it: the merged ``jobs`` row plus per-source extras (Simplify's
terms, degrees and category)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class JobView:
    id: int
    company: str
    title: str
    locations: list[str]
    description: str
    ats: str
    work_mode: str | None = None
    employment_type: str | None = None
    pay: dict[str, Any] | None = None
    posted_at: str | None = None
    first_seen: str = ""
    deadline: str | None = None
    live: bool = True
    terms: list[str] = field(default_factory=list)
    degrees: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Title, locations and description: what gates and recall read."""
        return "\n".join([self.title, " / ".join(self.locations), self.description]).strip()


def _row_to_view(row: sqlite3.Row, extras: dict[str, Any]) -> JobView:
    return JobView(
        id=row["id"],
        company=row["company"],
        title=row["title"],
        locations=[str(x) for x in json.loads(row["locations_json"] or "[]")],
        description=row["description"] or "",
        ats=row["ats"] or "unknown",
        work_mode=row["work_mode"],
        employment_type=row["employment_type"],
        pay=json.loads(row["pay_json"]) if row["pay_json"] else None,
        posted_at=row["posted_at"],
        first_seen=row["first_seen"],
        deadline=row["deadline"],
        live=bool(row["live"]),
        terms=extras.get("terms", []),
        degrees=extras.get("degrees", []),
        categories=extras.get("categories", []),
    )


def _extras(conn: sqlite3.Connection, job_ids: list[int] | None) -> dict[int, dict[str, Any]]:
    sql = "SELECT job_id, posting_json FROM source_jobs"
    params: tuple[Any, ...] = ()
    if job_ids is not None:
        if not job_ids:
            return {}
        sql += f" WHERE job_id IN ({','.join('?' * len(job_ids))})"
        params = tuple(job_ids)
    out: dict[int, dict[str, Any]] = {}
    for r in conn.execute(sql, params):
        extra = json.loads(r["posting_json"]).get("extra") or {}
        e = out.setdefault(r["job_id"], {"terms": [], "degrees": [], "categories": []})
        for src, dst in (("terms", "terms"), ("degrees", "degrees")):
            for v in extra.get(src) or []:
                if v not in e[dst]:
                    e[dst].append(v)
        if extra.get("category") and extra["category"] not in e["categories"]:
            e["categories"].append(extra["category"])
    return out


def load_jobs(
    conn: sqlite3.Connection, ids: Iterable[int] | None = None, *, live_only: bool = True
) -> list[JobView]:
    id_list = list(ids) if ids is not None else None
    sql = "SELECT * FROM jobs"
    clauses, params = [], []
    if live_only:
        clauses.append("live=1")
    if id_list is not None:
        if not id_list:
            return []
        clauses.append(f"id IN ({','.join('?' * len(id_list))})")
        params += id_list
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    extras = _extras(conn, [r["id"] for r in rows] if id_list is not None else None)
    return [_row_to_view(r, extras.get(r["id"], {})) for r in rows]
