"""Fit and tracker commands: gate, match, sync, run, jobs, show, mark, calibrate, eval-fit."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
from pydantic import BaseModel, Field

from jobhunt.cli.answers import _new_questions
from jobhunt.cli.discovery import FIXTURES_HELP, _prepare, make_client
from jobhunt.cli.framework import Result, agent_command
from jobhunt.config import load_user_file, load_watchlist, tracker_xlsx_path
from jobhunt.discovery.pipeline import FetchReport, plan_sources, run_fetch
from jobhunt.discovery.text import utc_now
from jobhunt.envelope import Envelope, paginate, render_human_default
from jobhunt.errors import NotFound
from jobhunt.fit import gates as gt
from jobhunt.fit.calibrate import CalibrationReport, calibrate
from jobhunt.fit.engine import (
    GateSummary,
    Inputs,
    MatchReport,
    run_gate_stage,
    run_match,
    update_gated_out,
)
from jobhunt.fit.jobview import load_jobs
from jobhunt.fit.recall import make_embedder
from jobhunt.locking import file_lock
from jobhunt.models import Answers, Config, Preferences, Profile
from jobhunt.notify.channels import configured
from jobhunt.notify.digest import after_run
from jobhunt.paths import Paths
from jobhunt.store import open_store
from jobhunt.tracker.backends import Backend, SheetsBackend, TrackerLocked, XlsxBackend
from jobhunt.tracker.states import machine
from jobhunt.tracker.store import mark
from jobhunt.tracker.sync import SyncReport, sync

PAGE = 20


def _inputs(paths: Paths, cfg: Config, now: str) -> Inputs:
    profile, prefs, answers = (
        load_user_file(k, paths) for k in ("profile", "preferences", "answers")
    )
    assert (
        isinstance(profile, Profile)
        and isinstance(prefs, Preferences)
        and isinstance(answers, Answers)
    )
    today = datetime.fromisoformat(now.replace("Z", "+00:00")).date()
    return Inputs(profile, prefs, answers, load_watchlist(paths), cfg, today, now)


def _lock(paths: Paths, dry_run: bool) -> Any:
    return nullcontext() if dry_run else file_lock(paths.lock)


def make_backend(cfg: Config, paths: Paths) -> Backend | None:
    t = cfg.tracker
    if t.kind == "none":
        return None
    if t.kind == "xlsx":
        return XlsxBackend(tracker_xlsx_path(cfg, paths))
    assert t.sheets.spreadsheet_id and t.sheets.service_account_file
    return SheetsBackend(t.sheets.spreadsheet_id, t.sheets.service_account_file)


def _do_sync(
    conn: Any, cfg: Config, paths: Paths, inp: Inputs, dry_run: bool
) -> tuple[SyncReport | None, list[str]]:
    try:
        backend = make_backend(cfg, paths)
    except TrackerLocked as e:
        return SyncReport(
            backend=cfg.tracker.kind, location="", dry_run=dry_run, skipped=e.message
        ), e.user_actions
    if backend is None:
        return None, []
    try:
        rep = sync(
            conn,
            backend,
            now=inp.now,
            today=inp.today,
            window_days=cfg.volume_guard.window_days,
            include_gated_out=cfg.tracker.include_gated_out,
            dry_run=dry_run,
        )
    except TrackerLocked as e:
        return SyncReport(
            backend=backend.name, location=backend.location, dry_run=dry_run, skipped=e.message
        ), e.user_actions
    actions = []
    if rep.rejected:
        actions.append(
            f"{len(rep.rejected)} sheet value(s) weren't applied; fix them in the tracker: "
            + "; ".join(rep.rejected[:5])
        )
    return rep, actions


def _question_actions(g: GateSummary) -> list[str]:
    if not g.user_questions:
        return []
    top = "; ".join(f"{q} ({n} jobs)" for q, n in list(g.user_questions.items())[:5])
    return [f"Some jobs need an answer only you can give (in the local form, not in chat): {top}"]


# --- output models --------------------------------------------------------------------------


class GatedJob(BaseModel):
    id: int
    company: str
    title: str
    reasons: list[str]


class GateReport(BaseModel):
    dry_run: bool
    gates: GateSummary
    gated_out: list[GatedJob] = Field(description=f"First {PAGE} gated-out jobs with reasons.")


class JobRow(BaseModel):
    id: int
    company: str
    title: str
    status: str | None
    band: str | None
    provisional: bool | None
    priority: float | None
    gate_status: str | None
    gate_reasons: list[str] = Field(default_factory=list)
    live: bool


class JobsList(BaseModel):
    jobs: list[JobRow]


class RunReport(BaseModel):
    dry_run: bool
    fetch: dict[str, Any]
    match: MatchReport
    forms: dict[str, Any] | None = Field(
        None, description="Application classes for the top jobs (`jobhunt inspect` for detail)."
    )
    sync: SyncReport | None
    notify: dict[str, Any] | None = Field(
        None, description="Digest counts, fast alerts sent, and where the digest was pushed."
    )


class MarkReport(BaseModel):
    job_id: int
    dry_run: bool
    change: dict[str, str | None]


def _render_calibrate(env: Envelope) -> str:
    d = env.data
    if not isinstance(d, dict) or "bands" not in d:
        return render_human_default(env)

    def pct(v: float | None) -> str:
        return "  -" if v is None else f"{v:4.0%}"

    lines = [
        f"Applied {d['applied']}: {d['replies']} replies, {d['no_reply']} no reply yet "
        "(no reply never counts against fit)",
        f"{'band':<10} {'applied':>7} {'replies':>7} {'interview':>9} {'offer':>5}  label",
    ]
    for b in d["bands"]:
        label = (b["label"] or "").replace("_", " ")
        if b["compared_with"]:
            label += f" (vs {b['compared_with']})"
        lines.append(
            f"{b['band']:<10} {b['applied']:>7} {b['replies']:>7} {pct(b['interview_rate']):>9} "
            f"{pct(b['offer_rate']):>5}  {label}"
        )
    if d["skip_reasons"]:
        lines.append(
            "Skipped: " + ", ".join(f"{k} {sum(v.values())}" for k, v in d["skip_reasons"].items())
        )
    if d["suggestions"]:
        lines.append("Suggestions (nothing is applied):")
        for s in d["suggestions"]:
            head = f"{s['key']}: {s['current']} -> {s['suggested']}. " if s["key"] else ""
            lines.append(f"  - {head}{s['reason']}")
    if d["config_snippet"]:
        lines += ["To accept, add to ~/.jobhunt/config.yaml:", d["config_snippet"].rstrip()]
    lines.append(d["note"])
    return "\n".join(lines)


def _reason_texts(reasons_json: str | None) -> list[str]:
    return [f"{r['gate']} {r['result']}: {r['reason']}" for r in json.loads(reasons_json or "[]")]


def _render_match(env: Envelope) -> str:
    d = env.data
    if not isinstance(d, dict) or "top" not in d:
        return render_human_default(env)
    g = d["gates"]
    lines = [
        f"Gates: {g['passed']} passed, {g['unclear']} unclear, {g['failed']} gated out of "
        f"{g['jobs']} live jobs",
    ]
    if g["drops_by_gate"]:
        lines.append(
            "  dropped by: " + ", ".join(f"{k} {v}" for k, v in g["drops_by_gate"].items())
        )
    r = d["recall"]
    lines.append(
        f"Recall ({r['mode']}): kept {r['kept']} of {r['ranked']}"
        + (f" — {r['note']}" if r.get("note") else "")
    )
    lines.append(
        "Bands: "
        + ", ".join(f"{k} {v}" for k, v in sorted(d["bands"].items()))
        + f" ({d['provisional']} provisional)"
    )
    t = d["tasks"]
    lines.append(
        f"Judgment tasks: {t['written']} new, {t['replaced']} replaced, {t['pending_total']} "
        f"pending"
    )
    lines.append("\nTop priorities:")
    for j in d["top"]:
        band = (j["band"] or "?") + ("*" if j["provisional"] else "")
        lines.append(
            f"  #{j['id']:<5} {j['priority']:.2f}  {band:<11} {j['company']}: {j['title']}"
        )
    lines.append("  (* provisional: some evidence not judged yet)")
    for w in d["warnings"]:
        lines.append(f"WARN {w}")
    if env.hints.user_actions:
        lines += ["\nYou need to:", *[f"  - {a}" for a in env.hints.user_actions]]
    return "\n".join(lines)


def register(cli: click.Group) -> None:
    @agent_command(cli, "gate", output=GateReport, writes=True)
    def gate_cmd(dry_run: bool) -> Result:
        """Run the gates (Must preferences and knockout requirements) over every live job.

        Every gated-out job keeps its reason and the posting text that triggered it.
        `jobhunt match` runs the gates too; use this to re-check after editing preferences.
        """
        paths, cfg = _prepare(dry_run)
        now = utc_now()
        inp = _inputs(paths, cfg, now)
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            conn.execute("BEGIN IMMEDIATE")
            summary, by_job = run_gate_stage(conn, inp, load_jobs(conn))
            conn.execute("COMMIT")
            update_gated_out(conn)
            jobs = {j.id: j for j in load_jobs(conn, list(by_job))}
        gated = [
            GatedJob(
                id=jid,
                company=jobs[jid].company,
                title=jobs[jid].title,
                reasons=[f"{r.gate}: {r.reason}" for r in res if r.result == "fail"],
            )
            for jid, res in by_job.items()
            if gt.overall(res) == "failed"
        ]
        return Result(
            data=GateReport(dry_run=dry_run, gates=summary, gated_out=gated[:PAGE]),
            user_actions=_question_actions(summary),
            next_actions=["Run `jobhunt match` to rank the jobs that passed."],
        )

    @agent_command(cli, "match", output=MatchReport, writes=True, render=_render_match)
    def match_cmd(dry_run: bool) -> Result:
        """Gate, recall, extract requirements, score and rank every live job; write judgment tasks.

        Qualification band and preference score are separate; priority combines them with
        freshness, deadline, referral and watchlist. Bands are provisional until the judgment
        tasks in ~/.jobhunt/work/tasks/ are done.
        """
        paths, cfg = _prepare(dry_run)
        inp = _inputs(paths, cfg, utc_now())
        embedder, note = make_embedder(cfg.fit)
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            rep = run_match(
                conn,
                inp,
                tasks_dir=paths.tasks,
                results_dir=paths.results,
                embedder=embedder,
                embed_note=note,
                dry_run=dry_run,
            )
        nxt = ["Run `jobhunt sync` to update the tracker."]
        if rep.tasks["pending_total"]:
            nxt.insert(
                0,
                f"{rep.tasks['pending_total']} judgment task(s) wait in {paths.tasks} (the "
                f"jobhunt-judge skill does them).",
            )
        return Result(data=rep, next_actions=nxt, user_actions=_question_actions(rep.gates))

    @agent_command(cli, "sync", output=SyncReport, writes=True)
    def sync_cmd(dry_run: bool) -> Result:
        """Sync the tracker: push agent columns, pull your columns back (your edits win).

        Skips (and says so) when the XLSX is open in a spreadsheet app.
        """
        paths, cfg = _prepare(dry_run)
        inp = _inputs(paths, cfg, utc_now())
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            rep, actions = _do_sync(conn, cfg, paths, inp, dry_run)
        if rep is None:
            return Result(data=None, next_actions=["The tracker is off (tracker.kind: none)."])
        return Result(data=rep, user_actions=actions)

    @agent_command(cli, "run", output=RunReport, writes=True, render=_render_run)
    @click.option(
        "--fixtures",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help=FIXTURES_HELP,
    )
    @click.option("--no-sync", is_flag=True, help="Skip the tracker sync.")
    @click.option("--no-inspect", is_flag=True, help="Skip reading and classifying forms.")
    @click.option("--no-notify", is_flag=True, help="Skip fast alerts and the pushed digest.")
    def run_cmd(
        fixtures: Path | None, no_sync: bool, no_inspect: bool, no_notify: bool, dry_run: bool
    ) -> Result:
        """The scheduled pipeline: fetch → gate → recall → score → classify → notify → sync.

        No AI needed. Classify reads the application forms of the top jobs (cached for a week).
        Notify pushes fast alerts for high-priority new postings and, when due, the digest to
        your configured channels (ntfy, Discord, email); `jobhunt digest` shows it here.
        """
        from jobhunt.cli.apply import form_inputs, loader_factory, run_inspect, threshold_jobs

        paths, cfg = _prepare(dry_run)
        inp = _inputs(paths, cfg, utc_now())
        plan = plan_sources(cfg.discovery, inp.prefs, inp.watchlist)
        client = make_client(cfg, fixtures)
        embedder, note = make_embedder(cfg.fit)
        actions: list[str] = []
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            fetch, raised, status = run_fetch(
                conn, client, plan, cfg=cfg.discovery, now=inp.now, dry_run=dry_run
            )
            rep = run_match(
                conn,
                inp,
                tasks_dir=paths.tasks,
                results_dir=paths.results,
                embedder=embedder,
                embed_note=note,
                dry_run=dry_run,
            )
            forms = None
            if not no_inspect and cfg.forms.max_per_run:
                ids = threshold_jobs(conn, cfg.fit.priority_threshold, cfg.forms.max_per_run)
                irep = run_inspect(
                    conn,
                    ids,
                    form_inputs(paths, cfg, inp.now),
                    client,
                    loader_factory(cfg, client, fixtures),
                    paths=paths,
                    cfg=cfg,
                    refresh=False,
                    dry_run=dry_run,
                )
                if irep.descriptions_stored:  # new posting text: recompute fit for those jobs
                    rep = run_match(
                        conn,
                        inp,
                        tasks_dir=paths.tasks,
                        results_dir=paths.results,
                        embedder=embedder,
                        embed_note=note,
                        dry_run=dry_run,
                    )
                forms = {
                    k: getattr(irep, k)
                    for k in ("inspected", "by_class", "fetched", "cached", "new_questions")
                }
                actions += irep.warnings
            srep = None
            if not no_sync:
                srep, sync_actions = _do_sync(conn, cfg, paths, inp, dry_run)
                actions += sync_actions
            fetch = _refresh_yields(conn, fetch)
            notify = None
            if not no_notify:
                notify = after_run(
                    conn,
                    cfg,
                    configured(cfg.notifications, cfg.http.user_agent),
                    _new_questions(paths),
                    run_started=inp.now,
                    now=utc_now(),
                    dry_run=dry_run,
                )
                for ch, res in notify["digest_pushed"].items():
                    if res != "sent":
                        actions.append(f"The digest couldn't reach {ch} ({res}).")
        if raised.get("ai_instructions"):
            actions.append(
                f"{len(raised['ai_instructions'])} new posting(s) contain text aimed at AI "
                f"tools; jobhunt won't follow it. Read them yourself."
            )
        return Result(
            data=RunReport(
                dry_run=dry_run,
                fetch=_fetch_summary(fetch, status),
                match=rep,
                forms=forms,
                sync=srep,
                notify=notify,
            ),
            user_actions=actions + _question_actions(rep.gates),
            next_actions=[
                f"{rep.tasks['pending_total']} judgment task(s) pending; ask your AI tool to "
                f"run the jobhunt-daily skill."
            ]
            if rep.tasks["pending_total"]
            else [],
        )

    @agent_command(cli, "jobs", output=JobsList)
    @click.option(
        "--status", default=None, help="Only this application status (e.g. New, Applied)."
    )
    @click.option(
        "--band", type=click.Choice(["strong", "good", "stretch", "not_a_fit"]), default=None
    )
    @click.option("--gated", is_flag=True, help="Only gated-out jobs (with their reasons).")
    @click.option("--all", "all_", is_flag=True, help="Include gated-out and closed jobs.")
    @click.option("--limit", type=int, default=PAGE, help="Jobs per page.")
    @click.option("--cursor", default=None, help="Page cursor from a previous call.")
    def jobs_cmd(
        status: str | None,
        band: str | None,
        gated: bool,
        all_: bool,
        limit: int,
        cursor: str | None,
    ) -> Result:
        """List jobs by priority. Gated-out jobs always show why."""
        paths, _ = _prepare(True)
        where, params = [], []
        if gated:
            where.append("f.gate_status='failed'")
        elif not all_:
            where.append("j.live=1 AND coalesce(f.gate_status,'') != 'failed'")
        if status:
            where.append("coalesce(a.status, ?) = ?")
            params += [machine().initial, machine().get(status).name]
        if band:
            where.append("f.band=?")
            params.append(band)
        sql = (
            "SELECT j.id, j.company, j.title, j.live, a.status, f.band, f.provisional, f.priority, "
            "f.gate_status, f.gate_reasons_json FROM jobs j LEFT JOIN fits f ON f.job_id=j.id "
            "LEFT JOIN applications a ON a.job_id=j.id"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY coalesce(f.priority, -1) DESC, j.id"
        )
        with open_store(paths.db, dry_run=True) as conn:
            rows = conn.execute(sql, params).fetchall()
        chunk, page = paginate(rows, cursor, limit)
        out = [
            JobRow(
                id=r["id"],
                company=r["company"],
                title=r["title"],
                status=r["status"] or machine().initial,
                band=r["band"],
                provisional=None if r["provisional"] is None else bool(r["provisional"]),
                priority=r["priority"],
                gate_status=r["gate_status"],
                gate_reasons=_reason_texts(r["gate_reasons_json"]),
                live=bool(r["live"]),
            )
            for r in chunk
        ]
        return Result(data=JobsList(jobs=out), page=page)

    @agent_command(cli, "show", output=None)
    @click.argument("job_id", type=int)
    def show_cmd(job_id: int) -> Result:
        """Everything about one job: gates with evidence, requirements, scores, signals, events."""
        paths, _ = _prepare(True)
        with open_store(paths.db, dry_run=True) as conn:
            data = job_detail(conn, job_id)
        return Result(data=data)

    @agent_command(cli, "mark", output=MarkReport, writes=True)
    @click.argument("job_id", type=int)
    @click.argument("status")
    @click.option("--reason", default=None, help="Why you're skipping (Skipped only).")
    @click.option(
        "--applied-on", default=None, help="Date you applied, YYYY-MM-DD (default today)."
    )
    def mark_cmd(
        job_id: int, status: str, reason: str | None, applied_on: str | None, dry_run: bool
    ) -> Result:
        """Set a job's status as the user (any status; Skipped needs --reason)."""
        paths, _ = _prepare(dry_run)
        now = utc_now()
        today = datetime.now(UTC).date()
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            change = mark(
                conn, job_id, status, reason=reason, applied_on=applied_on, today=today, now=now
            )
        return Result(
            data=MarkReport(job_id=job_id, dry_run=dry_run, change=change),
            next_actions=["Run `jobhunt sync` to update the tracker."],
        )

    @agent_command(cli, "calibrate", output=CalibrationReport, render=_render_calibrate)
    def calibrate_cmd() -> Result:
        """Interview and offer rates by band from your tracker, with suggestions. Never tunes."""
        paths, cfg = _prepare(True)
        with open_store(paths.db, dry_run=True) as conn:
            report = calibrate(conn, cfg)
        next_actions = []
        if report.config_snippet:
            next_actions.append(
                "Show the suggestions to the user. Only if they approve, they add config_snippet "
                "to ~/.jobhunt/config.yaml themselves, then `jobhunt match`."
            )
        if not report.replies:
            next_actions.append(
                "Record outcomes with `jobhunt mark <id> <status>` or in the tracker's Status "
                "column, then `jobhunt sync`."
            )
        return Result(data=report, next_actions=next_actions)

    @agent_command(cli, "eval-fit", output=None)
    @click.option(
        "--set",
        "set_dir",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help="Eval set directory (default: evals/fit in a jobhunt checkout).",
    )
    @click.option(
        "--personal",
        is_flag=True,
        help="Evaluate against your own labels (~/.jobhunt/labels.yaml).",
    )
    def eval_fit_cmd(set_dir: Path | None, personal: bool) -> Result:
        """Evaluate the fit engine per stage: gates, recall, bands, requirements, nDCG@10."""
        from jobhunt.fit.evaluate import evaluate_personal, evaluate_set, find_eval_set

        if personal:
            paths, cfg = _prepare(True)
            return Result(data=evaluate_personal(paths, cfg))
        d = set_dir or find_eval_set()
        if d is None or not d.is_dir():
            raise NotFound(
                "No eval set found.",
                next_actions=["Pass --set <dir>, run from a jobhunt checkout, or use --personal."],
            )
        return Result(data=evaluate_set(d))


def _refresh_yields(conn: Any, fetch: FetchReport) -> FetchReport:
    """gated_out is known only after gates; read it back into the fetch report."""
    if fetch.run_id is None:
        rid = conn.execute("SELECT max(id) FROM runs WHERE command='fetch'").fetchone()[0]
    else:
        rid = fetch.run_id
    got = {
        r["source_id"]: r["gated_out"]
        for r in conn.execute(
            "SELECT source_id, gated_out FROM source_yields WHERE run_id=?", (rid,)
        )
    }
    for s in fetch.sources:
        s.gated_out = got.get(s.source_id, s.gated_out)
    return fetch


def _fetch_summary(fetch: FetchReport, status: str) -> dict[str, Any]:
    return {
        "status": status,
        "totals": fetch.totals,
        "sources": [
            {
                k: getattr(s, k)
                for k in (
                    "source_id",
                    "status",
                    "baseline",
                    "fetched",
                    "new",
                    "gated_out",
                    "errors",
                )
            }
            for s in fetch.sources
        ],
        "new_jobs_total": fetch.new_jobs_total,
        "warnings": fetch.warnings,
    }


def _render_run(env: Envelope) -> str:
    d = env.data
    if not isinstance(d, dict) or "match" not in d:
        return render_human_default(env)
    f = d["fetch"]
    t = f["totals"]
    lines = [
        f"Fetch ({f['status']}): {t.get('fetched', 0)} fetched, {t.get('new', 0)} new, "
        f"{t.get('closed', 0)} closed"
    ]
    for s in f["sources"]:
        lines.append(
            f"  {s['source_id'][:40]:<42}{s['status']:<13} fetched {s['fetched']:>5}  new "
            f"{s['new']:>4}  gated out {s['gated_out']:>4}"
        )
    m = Envelope(ok=True, data=d["match"])
    lines.append("")
    lines.append(_render_match(m))
    if fm := d.get("forms"):
        lines.append(
            f"\nForms: {fm['inspected']} classified ("
            + ", ".join(f"{k} {v}" for k, v in sorted(fm["by_class"].items()))
            + f"); {fm['new_questions']} unanswered question(s)"
        )
    s = d.get("sync")
    if s:
        lines.append("")
        lines.append(
            f"Tracker: {s['skipped']}"
            if s.get("skipped")
            else f"Tracker ({s['backend']}): {s['rows_added']} added, {s['rows_updated']} updated, "
            f"{len(s['user_edits_applied'])} of your edits applied — {s['location']}"
        )
    if env.hints.user_actions:
        lines += ["\nYou need to:", *[f"  - {a}" for a in env.hints.user_actions]]
    return "\n".join(lines)


def job_detail(conn: Any, job_id: int) -> dict[str, Any]:
    j = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not j:
        raise NotFound(f"No job {job_id}.", next_actions=["Run `jobhunt jobs` to list job ids."])
    f = conn.execute("SELECT * FROM fits WHERE job_id=?", (job_id,)).fetchone()
    a = conn.execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchone()

    def js(v: str | None) -> Any:
        return json.loads(v) if v else None

    return {
        "job": {
            "id": j["id"],
            "company": j["company"],
            "title": j["title"],
            "url": j["canonical_url"],
            "ats": j["ats"],
            "locations": js(j["locations_json"]),
            "work_mode": j["work_mode"],
            "employment_type": j["employment_type"],
            "pay": js(j["pay_json"]),
            "posted_at": j["posted_at"],
            "first_seen": j["first_seen"],
            "deadline": j["deadline"],
            "live": bool(j["live"]),
            "closed_at": j["closed_at"],
        },
        "gate_status": f["gate_status"] if f else None,
        "gates": [
            dict(r)
            for r in conn.execute(
                "SELECT gate, result, reason, evidence FROM gate_results WHERE job_id=? ORDER BY "
                "CASE result WHEN 'fail' THEN 0 WHEN 'unclear' THEN 1 WHEN 'pass' THEN 2 ELSE 3 "
                "END, gate",
                (job_id,),
            )
        ],
        "fit": None
        if not f
        else {
            "band": f["band"],
            "provisional": bool(f["provisional"]),
            "priority": f["priority"],
            "preference_score": f["preference_score"],
            "relevance": f["relevance"],
            "relevance_note": "recall relevance, not fit",
            "recall_rank": f["recall_rank"],
            "recalled": bool(f["recalled"]),
            "qualification": js(f["qualification_json"]),
            "preference": js(f["preference_json"]),
            "priority_inputs": js(f["priority_json"]),
            "flags": js(f["flags_json"]) or [],
            "ai_disagreement": js(f["disagreement"]),
            "narrative": js(f["narrative_json"]),
        },
        "requirements": [
            dict(r)
            for r in conn.execute(
                "SELECT seq, importance, type, text, status, tier, evidence, evidence_ref, "
                "note, source FROM "
                "requirements WHERE job_id=? ORDER BY source='ai' DESC, seq",
                (job_id,),
            )
        ],
        "signals": [
            dict(r)
            for r in conn.execute("SELECT kind, evidence FROM signals WHERE job_id=?", (job_id,))
        ],
        "application": dict(a) if a else None,
        "tasks": [
            dict(r)
            for r in conn.execute(
                "SELECT id, kind, status, path FROM tasks WHERE job_id=? ORDER BY created_at "
                "DESC LIMIT 5",
                (job_id,),
            )
        ],
        "events": [
            dict(r)
            for r in conn.execute(
                "SELECT kind, at, source_id, details_json FROM events WHERE job_id=? ORDER BY "
                "id DESC LIMIT 10",
                (job_id,),
            )
        ],
    }
