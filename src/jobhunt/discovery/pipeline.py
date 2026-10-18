"""Discovery pipeline: choose sources, fetch, dedupe, record presence and events, flag signals.

Rules (from the build spec):
  - posted_at comes from the source or is NULL; first_seen is when jobhunt first saw the job.
  - Dedupe on (ats, board_token, job_id), else the canonical URL. Never on company + title.
  - A source's first successful fetch is a silent baseline: its appeared events are marked
    ``baseline`` and never alert.
  - Closures come from re-polling: a job missing from a successful, complete fetch is no longer
    listed by that source; when no source lists it, it is marked not live. Nothing is deleted.
  - Every appeared / changed / disappeared / reappeared event goes to the append-only events table.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from jobhunt.discovery import signals as sig
from jobhunt.discovery.resolver import SUPPORTED, resolve
from jobhunt.discovery.sources import (
    PRIORITY,
    AshbySource,
    FetchResult,
    GreenhouseSource,
    LeverSource,
    ManualSource,
    ManualTarget,
    Posting,
    SimplifySource,
    Source,
    SourceError,
    fetch_single,
)
from jobhunt.discovery.text import content_hash, simhash
from jobhunt.errors import ExitCode, JobhuntError, NetworkError, NotFound
from jobhunt.http import HttpClient
from jobhunt.models import Preferences, Watchlist
from jobhunt.models.config import DiscoveryConfig

MANUAL_ID = "manual"
CHANGE_FIELDS = (
    "title",
    "locations",
    "description",
    "pay",
    "work_mode",
    "employment_type",
    "deadline",
)
NEW_JOBS_SHOWN = 50

# --- output models --------------------------------------------------------------------------


class SourceYield(BaseModel):
    source_id: str
    kind: str
    status: Literal["ok", "not_modified", "error"]
    baseline: bool = Field(False, description="First fetch: builds the baseline, no alerts.")
    fetched: int = 0
    new: int = 0
    gated_out: int = Field(0, description="Gates arrive in Milestone 2.")
    changed: int = 0
    closed: int = 0
    reopened: int = 0
    errors: int = 0
    error_detail: str | None = None
    empty_runs: int = 0
    closures_held: bool = Field(False, description="Empty result: closures not applied yet.")


class JobBrief(BaseModel):
    id: int
    company: str
    title: str
    url: str | None
    locations: list[str]
    posted_at: str | None
    first_seen: str
    signals: list[str] = Field(default_factory=list)


class Coverage(BaseModel):
    supported: bool
    live_jobs: int


class FetchReport(BaseModel):
    run_id: int | None
    dry_run: bool
    sources: list[SourceYield]
    totals: dict[str, int]
    new_jobs: list[JobBrief] = Field(description=f"Non-baseline new jobs (first {NEW_JOBS_SHOWN}).")
    new_jobs_total: int
    new_signals: dict[str, int] = Field(description="Signals first raised this run, by kind.")
    coverage: dict[str, Coverage] = Field(description="Live jobs by ATS.")
    unsupported_watchlist: list[dict[str, str]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SignalOut(BaseModel):
    kind: str
    evidence: str | None


class AddReport(BaseModel):
    job_id: int
    created: bool
    reopened: bool
    dry_run: bool
    dedupe_key: str
    ats: str
    supported: bool
    company: str
    title: str
    url: str | None
    locations: list[str]
    posted_at: str | None
    first_seen: str
    live: bool
    signals: list[SignalOut]


# --- choosing sources -----------------------------------------------------------------------


@dataclass
class SourcePlan:
    sources: list[Source] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unsupported: list[dict[str, str]] = field(default_factory=list)


def plan_sources(cfg: DiscoveryConfig, prefs: Preferences, watchlist: Watchlist) -> SourcePlan:
    """Direct ATS boards from the watchlist first, then Simplify feeds chosen by preferences.
    (Order matters: direct-ATS data is richer, and merges prefer it.)"""
    plan = SourcePlan()
    seen: set[str] = set()

    def add(src: Source) -> None:
        if src.id not in seen:
            seen.add(src.id)
            plan.sources.append(src)

    kinds = {"greenhouse": GreenhouseSource, "lever": LeverSource, "ashby": AshbySource}
    for c in watchlist.companies:
        ats, board, region = (c.ats or "").lower() or None, c.board_token, None
        if c.careers_url and not (ats and board):
            r = resolve(c.careers_url)
            ats, board, region = ats or r.ats, board or r.board_token, r.region
        if ats in kinds and board:
            cls = kinds[ats]
            add(
                cls(f"{ats}:{board.lower()}", {"board": board, "company": c.name, "region": region})
            )
        elif ats and ats not in SUPPORTED and ats != "unknown":
            plan.unsupported.append({"company": c.name, "ats": ats})
            plan.warnings.append(
                f"{c.name} uses {ats}, which jobhunt can't read yet; counted in coverage."
            )
        else:
            plan.warnings.append(
                f"Couldn't find a Greenhouse, Lever or Ashby board for {c.name}. If it uses one, "
                "set `ats` and `board_token` in watchlist.yaml."
            )

    seniority = set(prefs.roles.seniority.value) if prefs.roles.seniority else set()
    etypes = set(prefs.employment_types.value) if prefs.employment_types else set()
    sc = cfg.simplify
    if sc.enabled:
        if "internship" in seniority or etypes & {"internship", "co-op"}:
            add(
                SimplifySource(
                    f"simplify:{sc.internship_repo}",
                    {"repo": sc.internship_repo, "branch": sc.branch},
                )
            )
        if seniority & {"new_grad", "entry"}:
            add(
                SimplifySource(
                    f"simplify:{sc.new_grad_repo}", {"repo": sc.new_grad_repo, "branch": sc.branch}
                )
            )
    return plan


def manual_source(conn: sqlite3.Connection) -> ManualSource | None:
    """Re-poll supported-ATS jobs the user added by URL."""
    rows = conn.execute(
        "SELECT j.dedupe_key, j.ats, j.board_token, j.ats_job_id, j.canonical_url "
        "FROM source_jobs s "
        "JOIN jobs j ON j.id = s.job_id WHERE s.source_id=? AND s.present=1 "
        "AND j.board_token IS NOT NULL AND j.ats_job_id IS NOT NULL",
        (MANUAL_ID,),
    ).fetchall()
    targets = [
        ManualTarget(
            r["dedupe_key"],
            r["ats"],
            r["board_token"],
            r["ats_job_id"],
            resolve(r["canonical_url"] or "").region,
        )
        for r in rows
        if r["ats"] in SUPPORTED
    ]
    return ManualSource(MANUAL_ID, {}, targets) if targets else None


def kind_of(source_id: str) -> str:
    return source_id.split(":", 1)[0]


# --- ingest ---------------------------------------------------------------------------------


def _posting_view(p: Posting) -> dict[str, Any]:
    return p.model_dump(mode="json")


def _changed_fields(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    def norm(f: str, v: Any) -> Any:
        return content_hash(v) if f == "description" else v

    return [f for f in CHANGE_FIELDS if norm(f, old.get(f)) != norm(f, new.get(f))]


class Ingestor:
    def __init__(self, conn: sqlite3.Connection, *, now: str, run_id: int, cfg: DiscoveryConfig):
        self.conn = conn
        self.now = now
        self.run_id = run_id
        self.cfg = cfg
        self.touched: set[int] = set()
        self.new_ids: list[int] = []  # non-baseline new jobs, for alerts
        self.created: set[int] = set()  # every job created this run
        self.fetched_boards: dict[tuple[str, str], str] = {}

    # sources table
    def ensure_source(self, src: Source) -> sqlite3.Row:
        self.conn.execute(
            "INSERT INTO sources (id, kind, config_json, baseline_done) VALUES (?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json",
            (
                src.id,
                src.kind,
                json.dumps(src.config, sort_keys=True),
                1 if src.kind == "manual" else 0,
            ),
        )
        return self.conn.execute("SELECT * FROM sources WHERE id=?", (src.id,)).fetchone()

    def event(self, job_id: int, kind: str, source_id: str, details: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO events (job_id, kind, at, source_id, run_id, details_json) "
            "VALUES (?,?,?,?,?,?)",
            (job_id, kind, self.now, source_id, self.run_id, json.dumps(details, sort_keys=True)),
        )

    def run_source(self, src: Source, client: HttpClient) -> SourceYield:
        row = self.ensure_source(src)
        y = SourceYield(
            source_id=src.id, kind=src.kind, status="ok", baseline=not row["baseline_done"]
        )
        try:
            result = src.fetch(client, etag=row["etag"], last_modified=row["last_modified"])
        except SourceError as e:
            y.status, y.errors, y.error_detail = "error", 1, str(e)
            y.empty_runs = row["empty_runs"]
            return y
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.ingest(src, row, result, y)
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return y

    def ingest(self, src: Source, row: sqlite3.Row, result: FetchResult, y: SourceYield) -> None:
        sid = src.id
        y.errors = len(result.errors)
        y.error_detail = "; ".join(result.errors[:3]) or None
        if result.not_modified:
            y.status = "not_modified"
        if result.not_modified and not row["empty_runs"]:
            # unchanged since the last full fetch: everything it listed is still listed
            y.empty_runs = row["empty_runs"]
            self.conn.execute(
                "UPDATE jobs SET last_checked=? WHERE id IN "
                "(SELECT job_id FROM source_jobs WHERE source_id=? AND present=1)",
                (self.now, sid),
            )
            self.conn.execute("UPDATE sources SET last_fetch_at=? WHERE id=?", (self.now, sid))
            self._note_board(src)
            return

        # (A 304 after an empty body is another empty run, so it counts toward releasing holds.)
        postings = _collapse(result.postings)
        y.fetched = len(postings)
        seen: set[int] = set()
        for p in postings:
            job_id, status = self.upsert(p, sid, baseline=y.baseline)
            seen.add(job_id)
            if status == "new":
                y.new += 1
            elif status == "changed":
                y.changed += 1
            elif status == "reopened":
                y.reopened += 1

        empty_runs = row["empty_runs"] + 1 if result.complete and not postings else 0
        y.empty_runs = empty_runs
        listed = [
            r[0]
            for r in self.conn.execute(
                "SELECT job_id FROM source_jobs WHERE source_id=? AND present=1", (sid,)
            )
        ]
        gone: list[int] = []
        if result.complete:
            gone = [j for j in listed if j not in seen]
            if not postings and gone and empty_runs < self.cfg.empty_run_warn_after:
                y.closures_held, gone = True, []
        for key in result.gone:
            r = self.conn.execute("SELECT id FROM jobs WHERE dedupe_key=?", (key,)).fetchone()
            if r and r["id"] in listed:
                gone.append(r["id"])
        for job_id in gone:
            if self.withdraw(job_id, sid):
                y.closed += 1

        self.conn.execute(
            "UPDATE sources SET baseline_done=1, etag=?, last_modified=?, last_fetch_at=?, "
            "empty_runs=? WHERE id=?",
            (result.etag, result.last_modified, self.now, empty_runs, sid),
        )
        self._note_board(src)

    def _note_board(self, src: Source) -> None:
        if src.kind in SUPPORTED:
            self.fetched_boards[(src.kind, src.config["board"].lower())] = src.id

    def find_job(self, p: Posting) -> sqlite3.Row | None:
        r = self.conn.execute("SELECT * FROM jobs WHERE dedupe_key=?", (p.dedupe_key,)).fetchone()
        if r:
            return r
        if p.ats in SUPPORTED and p.ats_job_id:
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE ats=? AND lower(ats_job_id)=lower(?)",
                (p.ats, p.ats_job_id),
            ).fetchall()
            if len(rows) == 1 and (
                not p.board_token
                or not rows[0]["board_token"]
                or rows[0]["board_token"].lower() == p.board_token.lower()
            ):
                return rows[0]
        return self.conn.execute(
            "SELECT j.* FROM job_urls u JOIN jobs j ON j.id=u.job_id WHERE u.url=? LIMIT 1",
            (p.canonical_url,),
        ).fetchone()

    def upsert(self, p: Posting, sid: str, *, baseline: bool) -> tuple[int, str]:
        """Insert or merge one posting. Returns (job id, new|changed|reopened|same)."""
        view = _posting_view(p)
        existing = self.find_job(p)
        if existing is None:
            cur = self.conn.execute(
                "INSERT INTO jobs (dedupe_key, company, title, first_seen, last_seen, "
                "last_checked) "
                "VALUES (?,?,?,?,?,?)",
                (p.dedupe_key, p.company, p.title, self.now, self.now, self.now),
            )
            job_id = int(cur.lastrowid or 0)
            self._presence(job_id, sid, view, new=True)
            self._urls(job_id, p, sid)
            self.refresh(job_id)
            self.event(
                job_id,
                "appeared",
                sid,
                {"baseline": baseline, "company": p.company, "title": p.title},
            )
            self.touched.add(job_id)
            self.created.add(job_id)
            if not baseline and sid != MANUAL_ID:
                self.new_ids.append(job_id)
            return job_id, "new"

        job_id = existing["id"]
        if existing["dedupe_key"].startswith("url:") and not p.dedupe_key.startswith("url:"):
            self.conn.execute("UPDATE jobs SET dedupe_key=? WHERE id=?", (p.dedupe_key, job_id))
        prev = self.conn.execute(
            "SELECT posting_json FROM source_jobs WHERE source_id=? AND job_id=?", (sid, job_id)
        ).fetchone()
        changed = _changed_fields(json.loads(prev["posting_json"]), view) if prev else []
        self._presence(job_id, sid, view, new=prev is None)
        self._urls(job_id, p, sid)
        self.refresh(job_id)
        if not existing["live"]:
            self.conn.execute("UPDATE jobs SET live=1, closed_at=NULL WHERE id=?", (job_id,))
            self.event(job_id, "reappeared", sid, {"closed_at": existing["closed_at"]})
            self.touched.add(job_id)
            return job_id, "reopened"
        if changed:
            self.event(job_id, "changed", sid, {"fields": changed})
            self.touched.add(job_id)
            return job_id, "changed"
        return job_id, "same"

    def _presence(self, job_id: int, sid: str, view: dict[str, Any], *, new: bool) -> None:
        if new:
            self.conn.execute(
                "INSERT INTO source_jobs (source_id, job_id, present, first_seen, last_seen, "
                "posting_json) "
                "VALUES (?,?,1,?,?,?)",
                (sid, job_id, self.now, self.now, json.dumps(view, sort_keys=True)),
            )
        else:
            self.conn.execute(
                "UPDATE source_jobs SET present=1, last_seen=?, posting_json=? "
                "WHERE source_id=? AND job_id=?",
                (self.now, json.dumps(view, sort_keys=True), sid, job_id),
            )

    def _urls(self, job_id: int, p: Posting, sid: str) -> None:
        for url in dict.fromkeys([p.canonical_url, p.url]):
            self.conn.execute(
                "INSERT OR IGNORE INTO job_urls (job_id, url, source_id, first_seen) "
                "VALUES (?,?,?,?)",
                (job_id, url, sid, self.now),
            )

    def refresh(self, job_id: int) -> None:
        """Recompute the job's merged fields from every source's view: listed before unlisted,
        then direct ATS > manual > Simplify, then most recent."""
        rows = self.conn.execute(
            "SELECT source_id, present, last_seen, posting_json FROM source_jobs WHERE job_id=?",
            (job_id,),
        ).fetchall()
        rows = sorted(
            rows,
            key=lambda r: (r["present"], PRIORITY.get(kind_of(r["source_id"]), 0), r["last_seen"]),
            reverse=True,
        )
        views = [json.loads(r["posting_json"]) for r in rows]

        def pick(f: str) -> Any:
            return next((v[f] for v in views if v.get(f)), None)

        ident = next((v for v in views if v["ats"] in SUPPORTED and v.get("ats_job_id")), views[0])
        desc = pick("description")
        self.conn.execute(
            "UPDATE jobs SET ats=?, board_token=?, ats_job_id=?, canonical_url=?, company=?, "
            "title=?, locations_json=?, work_mode=?, employment_type=?, description=?, "
            "description_hash=?, simhash=?, pay_json=?, posted_at=?, deadline=?, last_seen=?, "
            "last_checked=? WHERE id=?",
            (
                ident["ats"],
                ident.get("board_token"),
                ident.get("ats_job_id"),
                pick("canonical_url"),
                pick("company"),
                pick("title"),
                json.dumps(pick("locations") or []),
                pick("work_mode"),
                pick("employment_type"),
                desc,
                content_hash(desc),
                simhash(desc),
                json.dumps(pick("pay")) if pick("pay") else None,
                pick("posted_at"),
                pick("deadline"),
                self.now,
                self.now,
                job_id,
            ),
        )

    def withdraw(self, job_id: int, sid: str) -> bool:
        """This source no longer lists the job. True if that closed it (no source lists it)."""
        self.conn.execute(
            "UPDATE source_jobs SET present=0 WHERE source_id=? AND job_id=?", (sid, job_id)
        )
        others = self.conn.execute(
            "SELECT 1 FROM source_jobs WHERE job_id=? AND present=1", (job_id,)
        ).fetchone()
        self.refresh_keep_seen(job_id)
        if others:
            return False
        job = self.conn.execute("SELECT live FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job["live"]:
            return False
        self.conn.execute(
            "UPDATE jobs SET live=0, closed_at=?, last_checked=? WHERE id=?",
            (self.now, self.now, job_id),
        )
        self.event(job_id, "disappeared", sid, {})
        return True

    def refresh_keep_seen(self, job_id: int) -> None:
        last_seen = self.conn.execute(
            "SELECT last_seen FROM jobs WHERE id=?", (job_id,)
        ).fetchone()[0]
        self.refresh(job_id)
        self.conn.execute("UPDATE jobs SET last_seen=? WHERE id=?", (last_seen, job_id))

    # signals
    def flag(self) -> dict[str, list[int]]:
        """Raise signals for jobs touched this run, plus old_posting for every live job."""
        raised: dict[str, list[int]] = {}

        def note(kinds: Iterable[str], job_id: int) -> None:
            for k in kinds:
                raised.setdefault(k, []).append(job_id)

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for job_id in sorted(self.touched):
                job = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                note(sig.text_signals(self.conn, job, self.now), job_id)
                note(
                    sig.repost(
                        self.conn, job, self.now, self.created, self.cfg.repost_max_distance
                    ),
                    job_id,
                )
            for job in self.conn.execute("SELECT id, posted_at FROM jobs WHERE live=1").fetchall():
                note(
                    sig.old_posting(self.conn, job, self.now, self.cfg.old_posting_days), job["id"]
                )
            for job_id in sig.missing_from_ats(self.conn, self.fetched_boards, self.now):
                note(["missing_from_ats"], job_id)
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return raised


def _collapse(postings: list[Posting]) -> list[Posting]:
    """One posting per dedupe key within a single fetch (an aggregator may list a role twice):
    keep the first, union the locations."""
    by_key: dict[str, Posting] = {}
    for p in postings:
        first = by_key.get(p.dedupe_key)
        if first is None:
            by_key[p.dedupe_key] = p
        else:
            first.locations = list(dict.fromkeys([*first.locations, *p.locations]))
    return list(by_key.values())


# --- commands -------------------------------------------------------------------------------


def start_run(conn: sqlite3.Connection, command: str, now: str) -> int:
    cur = conn.execute("INSERT INTO runs (command, started_at) VALUES (?, ?)", (command, now))
    return int(cur.lastrowid or 0)


def finish_run(
    conn: sqlite3.Connection, run_id: int, status: str, stats: dict[str, Any], now: str
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, status=?, stats_json=? WHERE id=?",
        (now, status, json.dumps(stats, sort_keys=True), run_id),
    )


def job_brief(conn: sqlite3.Connection, job_id: int) -> JobBrief:
    j = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    kinds = [
        r[0]
        for r in conn.execute("SELECT kind FROM signals WHERE job_id=? ORDER BY kind", (job_id,))
    ]
    return JobBrief(
        id=j["id"],
        company=j["company"],
        title=j["title"],
        url=j["canonical_url"],
        locations=json.loads(j["locations_json"]),
        posted_at=j["posted_at"],
        first_seen=j["first_seen"],
        signals=kinds,
    )


def coverage(conn: sqlite3.Connection) -> dict[str, Coverage]:
    rows = conn.execute(
        "SELECT coalesce(ats, 'unknown') AS ats, count(*) AS n FROM jobs WHERE live=1 "
        "GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    return {r["ats"]: Coverage(supported=r["ats"] in SUPPORTED, live_jobs=r["n"]) for r in rows}


def run_fetch(
    conn: sqlite3.Connection,
    client: HttpClient,
    plan: SourcePlan,
    *,
    cfg: DiscoveryConfig,
    now: str,
    only: set[str] | None = None,
    dry_run: bool = False,
) -> tuple[FetchReport, dict[str, list[int]], str]:
    """Returns the report, the job ids per newly raised signal, and the run status."""
    run_id = start_run(conn, "fetch", now)
    ing = Ingestor(conn, now=now, run_id=run_id, cfg=cfg)
    sources = list(plan.sources)
    manual = manual_source(conn)
    if manual:  # after direct boards, before Simplify
        n_direct = sum(1 for s in sources if s.kind in SUPPORTED)
        sources.insert(n_direct, manual)
    if only:
        sources = [s for s in sources if s.id in only]
    yields = [ing.run_source(src, client) for src in sources]
    for y in yields:
        conn.execute(
            "INSERT INTO source_yields (run_id, source_id, fetched, new, gated_out, errors, "
            "error_detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, y.source_id, y.fetched, y.new, y.gated_out, y.errors, y.error_detail),
        )
    raised = ing.flag()

    warnings = list(plan.warnings)
    for y in yields:
        if y.status == "error":
            warnings.append(f"{y.source_id} failed: {y.error_detail}")
        if y.empty_runs >= cfg.empty_run_warn_after:
            warnings.append(
                f"{y.source_id} returned no jobs {y.empty_runs} runs in a row; it may be broken or "
                "the board name may have changed."
            )
        elif y.closures_held:
            warnings.append(
                f"{y.source_id} returned no jobs; jobs it listed stay open until it is empty "
                f"{cfg.empty_run_warn_after} runs in a row."
            )
    totals = {
        k: sum(getattr(y, k) for y in yields)
        for k in ("fetched", "new", "changed", "closed", "reopened", "errors")
    }
    failed = [y for y in yields if y.status == "error"]
    status = "failed" if yields and len(failed) == len(yields) else ("partial" if failed else "ok")
    finish_run(
        conn,
        run_id,
        status,
        {"totals": totals, "signals": {k: len(v) for k, v in raised.items()}},
        now,
    )
    return (
        FetchReport(
            run_id=None if dry_run else run_id,
            dry_run=dry_run,
            sources=yields,
            totals=totals,
            new_jobs=[job_brief(conn, j) for j in ing.new_ids[:NEW_JOBS_SHOWN]],
            new_jobs_total=len(ing.new_ids),
            new_signals={k: len(v) for k, v in sorted(raised.items())},
            coverage=coverage(conn),
            unsupported_watchlist=plan.unsupported,
            warnings=warnings,
        ),
        raised,
        status,
    )


class MissingPostingInfo(JobhuntError):
    code = "missing_posting_info"
    exit_code = ExitCode.USAGE


def add_url(
    conn: sqlite3.Connection,
    client: HttpClient,
    url: str,
    *,
    cfg: DiscoveryConfig,
    now: str,
    company: str | None = None,
    title: str | None = None,
    dry_run: bool = False,
) -> AddReport:
    r = resolve(url)
    unreadable: str | None = None
    if r.supported and r.board_token and r.job_id:
        target = ManualTarget(r.dedupe_key, r.ats, r.board_token, r.job_id, r.region)
        try:
            p: Posting | None = fetch_single(client, target, MANUAL_ID)
        except SourceError as e:
            if e.unreachable:
                raise NetworkError(f"Could not fetch the posting: {e}") from e
            # e.g. the board's API name differs from the slug in the job URL
            unreadable, p = str(e), None
    if unreadable is None and r.supported and r.board_token and r.job_id:
        if p is None:
            raise NotFound(
                f"{r.ats} says this posting does not exist; it may have closed.",
                details={"url": url, "dedupe_key": r.dedupe_key},
            )
        if company:
            p.company = company
        if title:
            p.title = title
    elif r.supported and not r.job_id and r.board_token:
        raise MissingPostingInfo(
            "That is a whole job board, not one posting.",
            details={"ats": r.ats, "board_token": r.board_token},
            next_actions=[f"Add the company to watchlist.yaml with careers_url: {r.canonical_url}"],
        )
    else:
        if not (company and title):
            why = (
                f"{r.ats}'s public API couldn't return this posting ({unreadable})"
                if unreadable
                else f"jobhunt can't read {r.ats} postings"
            )
            raise MissingPostingInfo(
                f"{why}, so it needs the company and title.",
                details={"ats": r.ats, "url": r.canonical_url},
                next_actions=[
                    "Re-run `jobhunt add <url> --company <name> --title <title>` using the "
                    "company and title shown on the posting."
                ],
            )
        p = Posting(
            source_id=MANUAL_ID,
            url=url,
            canonical_url=r.canonical_url,
            ats=r.ats,
            board_token=None if unreadable else r.board_token,  # keyed by URL: not re-pollable
            ats_job_id=r.job_id,
            company=company.strip(),
            title=title.strip(),
        )

    run_id = start_run(conn, "add", now)
    ing = Ingestor(conn, now=now, run_id=run_id, cfg=cfg)
    src = ManualSource(MANUAL_ID, {}, [])
    ing.ensure_source(src)
    conn.execute("BEGIN IMMEDIATE")
    try:
        job_id, status = ing.upsert(p, MANUAL_ID, baseline=False)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    ing.touched.add(job_id)
    ing.flag()
    finish_run(conn, run_id, "ok", {"job_id": job_id, "status": status}, now)

    j = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    sigs = [
        SignalOut(kind=s["kind"], evidence=s["evidence"])
        for s in conn.execute(
            "SELECT kind, evidence FROM signals WHERE job_id=? ORDER BY kind", (job_id,)
        )
    ]
    return AddReport(
        job_id=job_id,
        created=status == "new",
        reopened=status == "reopened",
        dry_run=dry_run,
        dedupe_key=j["dedupe_key"],
        ats=j["ats"] or "unknown",
        supported=(j["ats"] in SUPPORTED),
        company=j["company"],
        title=j["title"],
        url=j["canonical_url"],
        locations=json.loads(j["locations_json"]),
        posted_at=j["posted_at"],
        first_seen=j["first_seen"],
        live=bool(j["live"]),
        signals=sigs,
    )
