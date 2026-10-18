"""`jobhunt sync`: push agent-owned columns, pull user-owned columns back. User edits win.

Each user-owned cell is compared three ways — the sheet now, the store now, and the value last
synced — so a sheet edit and a `jobhunt mark` are told apart:

  sheet changed since last sync          -> the sheet wins (applied to the store if valid)
  only the store changed (jobhunt mark)  -> pushed to the sheet
  both changed                           -> the sheet wins, and the conflict is reported

Rows are matched by the "jobhunt ID" column. Columns and rows jobhunt doesn't know are left alone.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field

from jobhunt.tracker.backends import Backend, Grid
from jobhunt.tracker.states import machine

ID_COL = "jobhunt ID"


@dataclass(frozen=True)
class Column:
    header: str
    owner: str  # agent | user
    field: str | None = None  # applications column for user-owned ones


COLUMNS: list[Column] = [
    Column(ID_COL, "agent"),
    Column("Company", "agent"),
    Column("Role", "agent"),
    Column("Status", "user", "status"),
    Column("Priority", "agent"),
    Column("Qualification band", "agent"),
    Column("Preference score", "agent"),
    Column("Locations", "agent"),
    Column("Pay", "agent"),
    Column("Deadline", "agent"),
    Column("Class", "agent"),
    Column("Posted at", "agent"),
    Column("First seen", "agent"),
    Column("Posting live?", "agent"),
    Column("Gate results", "agent"),
    Column("Why fit", "agent"),
    Column("Gaps", "agent"),
    Column("Essay prompts", "agent"),
    Column("Legitimacy flags", "agent"),
    Column("Company brief", "agent"),
    Column("ATS", "agent"),
    Column("Job ID", "agent"),
    Column("Posting URL", "agent"),
    Column("Last checked", "agent"),
    Column("Applied on", "user", "applied_on"),
    Column("Referral contact", "user", "referral_contact"),
    Column("Next step", "user", "next_step"),
    Column("Follow-up on", "user", "follow_up_on"),
    Column("Outcome", "user", "outcome"),
    Column("Notes", "user", "notes"),
    Column("Skip reason", "user", "skip_reason"),
]
USER_COLUMNS = [c for c in COLUMNS if c.owner == "user"]
AGENT_COLUMNS = [c for c in COLUMNS if c.owner == "agent"]
DATE_FIELDS = {"applied_on", "follow_up_on"}


class SyncReport(BaseModel):
    backend: str
    location: str
    dry_run: bool
    rows_added: int = 0
    rows_updated: int = 0
    cells_pushed: int = 0
    user_edits_applied: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list, description="Both sides changed; sheet won.")
    rejected: list[str] = Field(default_factory=list, description="Sheet values not applied.")
    unknown_rows: int = Field(
        0, description="Rows whose jobhunt ID isn't in the store (left alone)."
    )
    skipped: str | None = None


# --- values ---------------------------------------------------------------------------------


def _date(ts: str | None) -> str:
    return (ts or "")[:10]


def _pay_text(pay_json: str | None) -> str:
    if not pay_json:
        return ""
    p = json.loads(pay_json)
    lo, hi, cur = p.get("min"), p.get("max"), p.get("currency") or ""
    if lo is None and hi is None:
        return p.get("text") or ""
    unit = (p.get("interval") or p.get("text") or "").lower()
    per = (
        "/hr"
        if re.search(r"hour|wage", unit)
        else "/yr"
        if re.search(r"year|annual|salary", unit)
        else ""
    )
    sym = "$" if cur in ("USD", "CAD", "") else f"{cur} "

    def fmt(v: float) -> str:
        return f"{sym}{v:,.0f}" if v >= 1000 else f"{sym}{v:g}"

    return (
        f"{fmt(lo)}–{fmt(hi)}"
        if lo is not None and hi is not None
        else fmt(lo if lo is not None else hi)
    ) + per


def _gate_text(status: str | None, reasons_json: str | None) -> str:
    if not status:
        return ""
    reasons = json.loads(reasons_json or "[]")
    if not reasons:
        return status
    parts = [f"{r['gate']}: {r['reason']}" for r in reasons]
    return f"{status} — " + "; ".join(parts)


def agent_values(conn: sqlite3.Connection, job_id: int) -> dict[str, str]:
    j = conn.execute(
        "SELECT j.*, f.band, f.provisional, f.priority, f.preference_score, f.gate_status, "
        "f.gate_reasons_json, f.recalled, f.flags_json, f.narrative_json, a.class FROM jobs j "
        "LEFT JOIN fits f ON f.job_id=j.id LEFT JOIN applications a ON a.job_id=j.id WHERE j.id=?",
        (job_id,),
    ).fetchone()
    reqs = conn.execute(
        "SELECT text, importance, status, tier FROM requirements WHERE job_id=? ORDER BY "
        "source='ai' DESC, seq",
        (job_id,),
    ).fetchall()
    why = [r["text"] for r in reqs if r["status"] == "met" and r["tier"] == "stated"][:3]
    unmet = [r["text"] for r in reqs if r["importance"] == "must" and r["status"] == "unmet"][:3]
    unassessed = sum(1 for r in reqs if r["status"] is None)
    narrative = json.loads(j["narrative_json"] or "null")
    if narrative:  # an ingested judgment: its cited reasons and its gaps
        why = [r["text"] for r in narrative["reasons"]] or why
        unmet = list(dict.fromkeys(unmet + narrative["gaps"]))[:5]
    gaps = "; ".join(unmet)
    if unassessed:
        gaps = (gaps + "; " if gaps else "") + f"{unassessed} requirement(s) not judged yet"
    signals = [
        r[0]
        for r in conn.execute("SELECT kind FROM signals WHERE job_id=? ORDER BY kind", (job_id,))
    ]
    flags = json.loads(j["flags_json"] or "[]")
    band = j["band"] or ""
    if band and j["provisional"]:
        band += " (provisional)"
    if j["gate_status"] == "failed":
        band = "gated out"
    brief = conn.execute(
        "SELECT brief_json FROM companies WHERE lower(name)=lower(?)", (j["company"],)
    ).fetchone()
    return {
        ID_COL: str(j["id"]),
        "Company": j["company"],
        "Role": j["title"],
        "Priority": f"{j['priority']:.2f}" if j["priority"] is not None else "",
        "Qualification band": band,
        "Preference score": f"{j['preference_score']:.2f}"
        if j["preference_score"] is not None
        else "",
        "Locations": "; ".join(json.loads(j["locations_json"] or "[]")),
        "Pay": _pay_text(j["pay_json"]) or ("not listed" if "pay not listed" in flags else ""),
        "Deadline": _date(j["deadline"]),
        "Class": {
            "ready": "Ready",
            "needs_answers": "Needs answers",
            "essay": "Essay",
            "manual": "Manual",
        }.get(j["class"] or "", ""),
        "Posted at": _date(j["posted_at"]),
        "First seen": _date(j["first_seen"]),
        "Posting live?": "Yes" if j["live"] else "No",
        "Gate results": _gate_text(j["gate_status"], j["gate_reasons_json"]),
        "Why fit": "; ".join(why),
        "Gaps": gaps,
        "Essay prompts": "",
        "Legitimacy flags": ", ".join(signals + [f for f in flags if f.startswith("volume guard")]),
        "Company brief": (json.loads(brief[0]).get("summary", "") if brief and brief[0] else ""),
        "ATS": j["ats"] or "unknown",
        "Job ID": j["ats_job_id"] or "",
        "Posting URL": j["canonical_url"] or "",
        "Last checked": _date(j["last_checked"]),
    }


def tracked_job_ids(conn: sqlite3.Connection, include_gated_out: bool) -> list[int]:
    """Jobs that belong in the sheet: matched (not gated out) jobs, plus any job you've acted on."""
    gate_clause = "" if include_gated_out else "AND coalesce(f.gate_status,'') != 'failed'"
    rows = conn.execute(
        "SELECT j.id FROM jobs j LEFT JOIN fits f ON f.job_id=j.id "
        "LEFT JOIN applications a ON a.job_id=j.id "
        f"WHERE (j.live=1 AND f.job_id IS NOT NULL {gate_clause}) "
        "OR (a.job_id IS NOT NULL AND a.status != 'New') "
        "ORDER BY coalesce(f.priority, 0) DESC, j.id"
    ).fetchall()
    return [r[0] for r in rows]


# --- user-owned values ----------------------------------------------------------------------


def _ensure_application(conn: sqlite3.Connection, job_id: int, now: str) -> sqlite3.Row:
    conn.execute(
        "INSERT OR IGNORE INTO applications (job_id, status, updated_at) VALUES (?, ?, ?)",
        (job_id, machine().initial, now),
    )
    return conn.execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchone()


def _normalize_user(field_name: str, raw: str) -> tuple[str | None, str | None]:
    """(value for the store, error). Empty -> None."""
    v = raw.strip()
    if field_name == "status":
        if not v:
            return machine().initial, None
        if not machine().is_valid(v):
            return None, f"unknown status {v!r} (valid: {', '.join(machine().states)})"
        return machine().get(v).name, None
    if field_name == "skip_reason" and v and v not in machine().skip_reasons:
        return None, f"skip reason {v!r} not one of {', '.join(machine().skip_reasons)}"
    if field_name in DATE_FIELDS and v:
        try:
            return date.fromisoformat(v[:10]).isoformat(), None
        except ValueError:
            return None, f"{v!r} is not a date (use YYYY-MM-DD)"
    return (v or None), None


def set_user_field(
    conn: sqlite3.Connection,
    job_id: int,
    field_name: str,
    value: str | None,
    now: str,
    *,
    source: str,
) -> None:
    old = conn.execute(
        f"SELECT {field_name} FROM applications WHERE job_id=?", (job_id,)
    ).fetchone()[0]
    if old == value:
        return
    conn.execute(
        f"UPDATE applications SET {field_name}=?, updated_at=? WHERE job_id=?", (value, now, job_id)
    )
    if field_name == "status":
        conn.execute(
            "INSERT INTO events (job_id, kind, at, source_id, details_json) VALUES (?, "
            "'status_changed', ?, ?, ?)",
            (job_id, now, source, json.dumps({"from": old, "to": value})),
        )


def _last_synced(conn: sqlite3.Connection, job_id: int) -> dict[str, str | None]:
    return {
        r["col"]: r["value"]
        for r in conn.execute("SELECT col, value FROM tracker_sync WHERE job_id=?", (job_id,))
    }


def _record(conn: sqlite3.Connection, job_id: int, col: str, value: str | None, now: str) -> None:
    conn.execute(
        "INSERT INTO tracker_sync (job_id, col, value, synced_at) VALUES (?,?,?,?) "
        "ON CONFLICT(job_id, col) DO UPDATE SET value=excluded.value, synced_at=excluded.synced_at",
        (job_id, col, value, now),
    )


# --- sync -----------------------------------------------------------------------------------


def _ensure_header(grid: Grid, rows: list[list[str]]) -> dict[str, int]:
    if not rows:
        grid.append_rows([[c.header for c in COLUMNS]])
        rows.append([c.header for c in COLUMNS])
    header = rows[0]
    colmap = {h.strip(): i + 1 for i, h in enumerate(header) if h.strip()}
    missing = [c.header for c in COLUMNS if c.header not in colmap]
    if missing:
        start = len(header) + 1
        grid.set_cells([(1, start + i, h) for i, h in enumerate(missing)])
        for i, h in enumerate(missing):
            colmap[h] = start + i
    return colmap


def sync_jobs(
    conn: sqlite3.Connection, grid: Grid, now: str, report: SyncReport, *, include_gated_out: bool
) -> None:
    rows = grid.values()
    colmap = _ensure_header(grid, rows)
    id_c = colmap[ID_COL]
    known = {r[0] for r in conn.execute("SELECT id FROM jobs")}
    in_sheet: dict[int, int] = {}
    for rn, row in enumerate(rows[1:], start=2):
        raw = row[id_c - 1] if len(row) >= id_c else ""
        if raw.isdigit() and int(raw) in known:
            in_sheet.setdefault(int(raw), rn)
        elif any(row):
            report.unknown_rows += 1
    updates: list[tuple[int, int, str]] = []

    for job_id, rn in in_sheet.items():
        row = rows[rn - 1]
        app = _ensure_application(conn, job_id, now)
        last = _last_synced(conn, job_id)
        touched = False
        for col in USER_COLUMNS:
            assert col.field
            c = colmap[col.header]
            sheet_raw = row[c - 1] if len(row) >= c else ""
            sheet_v, err = _normalize_user(col.field, sheet_raw)
            db_v = app[col.field]
            if err:
                report.rejected.append(f"job {job_id} {col.header}: {err}")
                continue
            if col.header not in last:
                # never synced (e.g. the sync record was lost): a filled-in cell wins
                if sheet_raw.strip() and sheet_v != db_v:
                    set_user_field(conn, job_id, col.field, sheet_v, now, source="tracker")
                    report.user_edits_applied.append(
                        f"job {job_id} {col.header}: {db_v!r} → {sheet_v!r}"
                    )
                    _record(conn, job_id, col.header, sheet_v, now)
                elif not sheet_raw.strip() and db_v:
                    updates.append((rn, c, db_v))
                    touched = True
                    _record(conn, job_id, col.header, db_v, now)
                else:
                    _record(conn, job_id, col.header, db_v, now)
                continue
            last_v = last[col.header]
            sheet_changed, db_changed = sheet_v != last_v, db_v != last_v
            if sheet_changed:
                if db_changed and db_v != sheet_v:
                    report.conflicts.append(
                        f"job {job_id} {col.header}: sheet {sheet_v!r} kept over {db_v!r}"
                    )
                if sheet_v != db_v:
                    set_user_field(conn, job_id, col.field, sheet_v, now, source="tracker")
                    report.user_edits_applied.append(
                        f"job {job_id} {col.header}: {db_v!r} → {sheet_v!r}"
                    )
                _record(conn, job_id, col.header, sheet_v, now)
            elif db_changed:
                updates.append((rn, c, db_v or ""))
                touched = True
                _record(conn, job_id, col.header, db_v, now)
        for header, value in agent_values(conn, job_id).items():
            c = colmap[header]
            cur = row[c - 1] if len(row) >= c else ""
            if cur != value:
                updates.append((rn, c, value))
                touched = True
        report.rows_updated += touched

    new_rows = []
    width = max(colmap.values())
    for job_id in tracked_job_ids(conn, include_gated_out):
        if job_id in in_sheet:
            continue
        app = _ensure_application(conn, job_id, now)
        vals = agent_values(conn, job_id)
        line = [""] * width
        for header, value in vals.items():
            line[colmap[header] - 1] = value
        for col in USER_COLUMNS:
            v = app[col.field] or ""
            line[colmap[col.header] - 1] = v
            _record(conn, job_id, col.header, app[col.field], now)
        new_rows.append(line)
    report.cells_pushed = len(updates)
    report.rows_added = len(new_rows)
    grid.set_cells(updates)
    grid.append_rows(new_rows)


def companies_rows(conn: sqlite3.Connection, today: date, window_days: int) -> list[list[str]]:
    from jobhunt.fit.engine import _norm_company, recent_applications

    applied = recent_applications(conn, today, window_days)
    rows = conn.execute(
        "SELECT j.company, sum(j.live) AS live, count(*) AS total, max(f.priority) AS top, "
        "group_concat(DISTINCT f.band) AS bands, c.brief_json FROM jobs j "
        "LEFT JOIN fits f ON f.job_id=j.id LEFT JOIN companies c ON lower(c.name)=lower(j.company) "
        "WHERE coalesce(f.gate_status,'') != 'failed' GROUP BY j.company ORDER BY top DESC, "
        "j.company"
    ).fetchall()
    out = [
        [
            "Company",
            "Live jobs",
            "Jobs seen",
            f"Applied ({window_days}d)",
            "Best band",
            "Top priority",
            "Brief",
        ]
    ]
    order = ["strong", "good", "stretch", "not_a_fit"]
    for r in rows:
        bands = [b for b in (r["bands"] or "").split(",") if b]
        best = next((b for b in order if b in bands), "")
        brief = json.loads(r["brief_json"]).get("summary", "") if r["brief_json"] else ""
        out.append(
            [
                r["company"],
                str(r["live"] or 0),
                str(r["total"]),
                str(applied.get(_norm_company(r["company"]), 0)),
                best,
                f"{r['top']:.2f}" if r["top"] is not None else "",
                brief,
            ]
        )
    return out


def runs_rows(conn: sqlite3.Connection, limit: int = 200) -> list[list[str]]:
    out = [
        [
            "Run",
            "Command",
            "Started",
            "Finished",
            "Status",
            "Summary",
            "Source yields (fetched/new/gated out/errors)",
        ]
    ]
    for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall():
        stats = json.loads(r["stats_json"] or "{}")
        if "totals" in stats:
            summary = ", ".join(f"{k} {v}" for k, v in stats["totals"].items())
        elif "gates" in stats:
            g = stats["gates"]
            summary = f"gates passed {g['passed']}, unclear {g['unclear']}, failed {g['failed']}"
            if stats.get("bands"):
                summary += "; bands " + ", ".join(f"{k} {v}" for k, v in stats["bands"].items())
        else:
            summary = ", ".join(f"{k} {v}" for k, v in stats.items() if not isinstance(v, dict))
        ys = conn.execute(
            "SELECT source_id, fetched, new, gated_out, errors FROM source_yields WHERE run_id=?",
            (r["id"],),
        ).fetchall()
        yields = "; ".join(
            f"{y['source_id']} {y['fetched']}/{y['new']}/{y['gated_out']}/{y['errors']}" for y in ys
        )
        out.append(
            [
                str(r["id"]),
                r["command"],
                _date(r["started_at"]) + r["started_at"][10:16].replace("T", " "),
                (r["finished_at"] or "")[:16].replace("T", " "),
                r["status"],
                summary,
                yields,
            ]
        )
    return out


class DryGrid:
    """Reads through to the real grid; writes go nowhere (for --dry-run)."""

    def __init__(self, inner: Grid) -> None:
        self.inner = inner

    def values(self) -> list[list[str]]:
        return self.inner.values()

    def set_cells(self, updates: list[tuple[int, int, str]]) -> None:
        return None

    def append_rows(self, rows: list[list[str]]) -> None:
        return None

    def replace_all(self, rows: list[list[str]]) -> None:
        return None


def sync(
    conn: sqlite3.Connection,
    backend: Backend,
    *,
    now: str,
    today: date,
    window_days: int,
    include_gated_out: bool,
    dry_run: bool,
) -> SyncReport:
    report = SyncReport(backend=backend.name, location=backend.location, dry_run=dry_run)

    def tab(title: str, **kw: object) -> Grid:
        g = backend.tab(title, **kw)  # type: ignore[arg-type]
        return DryGrid(g) if dry_run else g

    conn.execute("BEGIN IMMEDIATE")
    try:
        jobs = tab("Jobs", user_columns=[c.header for c in USER_COLUMNS])
        sync_jobs(conn, jobs, now, report, include_gated_out=include_gated_out)
        tab("Companies").replace_all(companies_rows(conn, today, window_days))
        tab("Runs").replace_all(runs_rows(conn))
        if dry_run:
            conn.execute("ROLLBACK")
            return report
        backend.save()
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return report
