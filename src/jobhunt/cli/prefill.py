"""`jobhunt prefill`: fill Ready jobs' forms in a real browser and stop before Submit.

There is no submit command. The human reviews each tab and clicks Submit themselves.
"""

from __future__ import annotations

import secrets
import sqlite3
from pathlib import Path
from typing import Any

import click
from pydantic import BaseModel, Field

from jobhunt.cli.apply import form_inputs
from jobhunt.cli.discovery import _prepare
from jobhunt.cli.framework import Result, agent_command
from jobhunt.cli.onboarding import (
    default_resume,
    listed_resumes,
    resolve_resume,
    suggest_resume,
)
from jobhunt.discovery.text import utc_now
from jobhunt.drafts.essays import approved_essays
from jobhunt.errors import LockBusy, NotFound, ValidationFailed
from jobhunt.fit.jobview import load_jobs
from jobhunt.forms.classify import classify
from jobhunt.forms.inspect import Inputs
from jobhunt.forms.model import FormSpec
from jobhunt.paths import Paths
from jobhunt.prefill import driver
from jobhunt.prefill.adapters import ADAPTERS
from jobhunt.prefill.browser import require_playwright
from jobhunt.prefill.review import TabReview
from jobhunt.prefill.values import build_sheet, read_sheet, validate, write_sheet
from jobhunt.store import open_store

DONE_STATES = ("Applied", "OA", "Interviewing", "Offer", "Rejected", "Withdrawn", "Skipped")


class PrefillJob(BaseModel):
    job_id: int
    company: str = ""
    title: str = ""
    ats: str = ""
    status: str = Field(
        description="queued | skipped | invalid | filling | ready_for_review | failed | applied "
        "| closed | stopped"
    )
    reason: str | None = None
    resume: str | None = Field(None, description="The resume chosen for this job (its name).")
    suggested_resume: str | None = Field(
        None, description="With several resumes and no default: the closest one, to confirm."
    )
    problems: list[str] = Field(default_factory=list, description="Validation problems.")
    review: TabReview | None = None


class PrefillReport(BaseModel):
    dry_run: bool
    batch_id: str | None = None
    driver: str = Field(
        "not_started",
        description="not_started | starting | filling | waiting_for_you | done | stopped",
    )
    running: bool = False
    jobs: list[PrefillJob] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


def ready_jobs(conn: sqlite3.Connection, limit: int) -> list[int]:
    """Ready jobs (and Essay jobs with approved essays) not yet pre-filled or applied to, highest
    priority first."""
    rows = conn.execute(
        "SELECT fs.job_id FROM form_specs fs JOIN jobs j ON j.id = fs.job_id "
        "LEFT JOIN applications a ON a.job_id = fs.job_id LEFT JOIN fits f ON f.job_id = fs.job_id "
        "WHERE (fs.class = 'ready' OR (fs.class = 'essay' AND EXISTS (SELECT 1 FROM drafts d "
        "WHERE d.job_id = fs.job_id AND d.kind = 'essay' AND d.status = 'approved'))) "
        "AND j.live = 1 AND coalesce(a.status, 'New') = 'New' "
        "AND coalesce(json_extract(fs.details_json, '$.filter'), '') != 'skip' "
        "ORDER BY coalesce(f.priority, 0) DESC, j.id LIMIT ?",
        (limit,),
    ).fetchall()
    return [r["job_id"] for r in rows]


def choose_resume(paths: Paths, job_id: int, given: Path | None) -> Path | None:
    """--resume, else the resume chosen for this job before (its answers.json), else the default
    or only one. None when there are several and none is the default."""
    if given:
        return given
    old = paths.prefill / str(job_id) / "answers.json"
    if old.exists():
        try:
            chosen = read_sheet(old).resume
        except Exception:
            chosen = None
        if chosen and Path(chosen).is_file():
            return Path(chosen)
    return default_resume(paths)


def prepare_job(
    conn: sqlite3.Connection,
    job_id: int,
    inp: Inputs,
    paths: Paths,
    now: str,
    *,
    dry_run: bool,
    resume: Path | None = None,
) -> tuple[PrefillJob, driver.BatchJob | None]:
    """Re-classify one job and build + validate its answers.json."""
    jobs = load_jobs(conn, [job_id], live_only=False)
    if not jobs:
        raise NotFound(f"No job {job_id}.", next_actions=["Run `jobhunt jobs`."])
    job = jobs[0]
    out = PrefillJob(job_id=job_id, company=job.company, title=job.title, status="skipped")
    app = conn.execute("SELECT status FROM applications WHERE job_id=?", (job_id,)).fetchone()
    if app and app["status"] in DONE_STATES:
        out.reason = f"already {app['status']}"
        return out, None
    row = conn.execute("SELECT * FROM form_specs WHERE job_id=?", (job_id,)).fetchone()
    if not row or not row["spec_json"]:
        out.reason = "form not read yet: run `jobhunt inspect` first"
        return out, None
    spec = FormSpec.model_validate_json(row["spec_json"])
    out.ats = spec.ats
    if spec.ats not in ADAPTERS:
        out.reason = f"no pre-fill adapter for {spec.ats}: use `jobhunt packet {job_id}`"
        return out, None
    ctx = inp.ctx(job)
    ctx.resume = choose_resume(paths, job_id, resume)
    if ctx.resume is None and listed_resumes(paths):
        out.suggested_resume = suggest_resume(paths, job.text)
        hint = f" (closest to this posting: {out.suggested_resume})" if out.suggested_resume else ""
        out.reason = (
            f"several resumes and no default: choose one with `jobhunt prefill {job_id} "
            f"--resume <file>`{hint}"
        )
        return out, None
    out.resume = ctx.resume.name if ctx.resume else None
    cls = classify(spec, ctx, inp.matcher, eeo_confirmed=inp.answers.eeo.confirmed)
    essays = approved_essays(conn, job_id)
    unapproved = [f for f in cls.essays if f.required and f.id not in essays]
    if cls.cls == "essay" and not unapproved and not cls.needs:
        pass  # every required essay is the user's approved text: fill like a Ready job
    elif cls.cls != "ready":
        out.reason = f"not Ready ({cls.cls}): " + "; ".join(cls.reasons)
        if unapproved:
            out.reason += f"; approve {len(unapproved)} essay(s) with `jobhunt essays approve`"
        return out, None
    url = spec.url or row["url"]
    if not url:
        out.reason = "the application page's URL is unknown"
        return out, None
    sheet = build_sheet(job_id, spec, cls, ctx, inp.matcher, now, essays)
    problems = validate(sheet, spec)
    if problems:
        out.status = "invalid"
        out.reason = "answers.json failed validation"
        out.problems = [f"{p.label}: {p.problem}" for p in problems]
        return out, None
    path = paths.prefill / str(job_id) / "answers.json"
    if not dry_run:
        write_sheet(path, sheet)
    out.status = "queued"
    return out, driver.BatchJob(
        job_id=job_id, company=job.company, title=job.title, ats=spec.ats, url=url,
        sheet=str(path),
    )  # fmt: skip


def _from_state(st: dict[str, Any]) -> list[PrefillJob]:
    out = []
    for t in st.get("tabs") or []:
        rev = TabReview.model_validate(t["review"]) if t.get("review") else None
        job = PrefillJob(
            job_id=t["job_id"], company=t.get("company", ""), title=t.get("title", ""),
            ats=t.get("ats", ""), status=t["status"], review=rev,
            reason=(rev.error if rev and rev.error else None),
        )  # fmt: skip
        out.append(job)
    return out


def _counts(jobs: list[PrefillJob]) -> dict[str, int]:
    c: dict[str, int] = {}
    for j in jobs:
        c[j.status] = c.get(j.status, 0) + 1
    return c


def status_result(paths: Paths, *, dry_run: bool = True) -> Result:
    st = driver.read_state(paths) or {}
    jobs = _from_state(st)
    alive = driver.running(paths) is not None
    rep = PrefillReport(
        dry_run=dry_run, batch_id=st.get("batch_id"), driver=st.get("status") or "not_started",
        running=alive, jobs=jobs, counts=_counts(jobs),
    )  # fmt: skip
    nxt, user = [], []
    review = [j for j in jobs if j.status == "ready_for_review"]
    if alive and review:
        user.append(
            f"{len(review)} tab(s) are filled and waiting in the jobhunt browser window. In each "
            "one, check the highlighted fields, then click Submit yourself. jobhunt never submits."
        )
    if alive:
        nxt.append("Poll `jobhunt prefill --status`; submitted tabs turn `applied`.")
    elif rep.driver in ("done", "stopped"):
        nxt.append("Run `jobhunt prefill` for the next batch; `jobhunt sync` updates the sheet.")
    return Result(data=rep, next_actions=nxt, user_actions=user)


def register(cli: click.Group) -> None:
    @agent_command(cli, "prefill", output=PrefillReport, writes=True)
    @click.argument("job_ids", nargs=-1, type=int)
    @click.option("--batch", type=int, default=None, help="Tabs per batch (default: config, 5).")
    @click.option("--status", "status_only", is_flag=True, help="Report the open batch.")
    @click.option("--stop", is_flag=True, help="Close the pre-fill browser (tabs stay Prefilled).")
    @click.option(
        "--fixtures",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help="Serve application pages from saved fixtures instead of the network (testing).",
    )
    @click.option(
        "--headless", is_flag=True, help="No visible window (testing only: you can't submit)."
    )
    @click.option(
        "--resume",
        "resume_name",
        default=None,
        help="The resume to attach (a file name in ~/.jobhunt/resume or a path). Kept for the "
        "job; default: the one chosen for it before, else your default resume.",
    )
    def prefill_cmd(
        job_ids: tuple[int, ...],
        batch: int | None,
        status_only: bool,
        stop: bool,
        fixtures: Path | None,
        headless: bool,
        resume_name: str | None,
        dry_run: bool,
    ) -> Result:
        """Fill Ready jobs' application forms in a browser window and stop before Submit.

        Code builds and validates each job's answers.json from your profile and answer bank,
        then fills one batch of tabs (default 5), reads every value back, highlights fields that
        need you and takes screenshots. You review each tab and click Submit yourself; jobhunt
        marks the job Applied when it sees the confirmation. Returns at once; poll `--status`.
        `--dry-run` builds and validates answers without opening a browser.
        """
        paths, cfg = _prepare(dry_run)
        if status_only:
            return status_result(paths, dry_run=dry_run)
        if stop:
            if not dry_run:
                driver.stop(paths)
            return status_result(paths, dry_run=dry_run)
        if (st := driver.running(paths)) is not None:
            raise LockBusy(
                f"A pre-fill batch is already open (driver pid {st.get('pid')}).",
                next_actions=[
                    "Run `jobhunt prefill --status`, or `jobhunt prefill --stop` to close it."
                ],
            )
        if not dry_run:
            require_playwright()
        now = utc_now()
        inp = form_inputs(paths, cfg, now)
        size = batch or cfg.prefill.batch_size
        chosen = resolve_resume(paths, resume_name) if resume_name else None
        with open_store(paths.db, dry_run=True) as conn:
            ids = list(job_ids) or ready_jobs(conn, size)
            prepared = [
                prepare_job(conn, i, inp, paths, now, dry_run=dry_run, resume=chosen) for i in ids
            ]
        report_jobs = [p for p, _ in prepared]
        queue = [b for _, b in prepared if b is not None][:size]
        rep = PrefillReport(dry_run=dry_run, jobs=report_jobs, counts=_counts(report_jobs))
        nxt: list[str] = []
        if not ids:
            nxt.append(
                "No Ready jobs to pre-fill. Run `jobhunt inspect`, then answer open questions "
                "with `jobhunt answers questions` so more jobs become Ready."
            )
        if any(j.reason and j.reason.startswith("several resumes") for j in report_jobs):
            nxt.append(
                "Ask the user which resume to send for each job (a suggestion is shown), then "
                "re-run with `--resume <file>`; the choice is kept for that job."
            )
        if any(j.status == "invalid" for j in report_jobs):
            nxt.append("Some answers failed validation; fix them with `jobhunt answers edit`.")
        if dry_run or not queue:
            return Result(data=rep, next_actions=nxt)
        plan = driver.Plan(
            batch_id=secrets.token_hex(4), created_at=now, jobs=queue, headless=headless,
            fixtures=str(fixtures.resolve()) if fixtures else None,
            parse_wait_s=cfg.prefill.parse_wait_s, watch_hours=cfg.prefill.watch_hours,
        )  # fmt: skip
        try:
            st = driver.launch(paths, plan)
        except RuntimeError as e:
            raise ValidationFailed(str(e)) from e
        rep.batch_id, rep.driver, rep.running = plan.batch_id, st.get("status", "starting"), True
        for j in rep.jobs:
            if j.status == "queued" and j.job_id not in {q.job_id for q in queue}:
                j.status, j.reason = "skipped", "over the batch size: next batch"
        rep.counts = _counts(rep.jobs)
        return Result(
            data=rep,
            next_actions=["Poll `jobhunt prefill --status` until the tabs are ready_for_review."],
            user_actions=[
                f"A jobhunt browser window is opening with {len(queue)} application tab(s). Wait "
                "until each is filled, check the highlighted fields, then click Submit yourself. "
                "jobhunt never submits, and marks a job Applied when it sees the confirmation."
            ],
        )
