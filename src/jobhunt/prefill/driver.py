"""The pre-fill driver: one batch of tabs, filled by code, submitted by the human.

``jobhunt prefill`` validates each job's answers.json, writes the batch plan, starts this module
as a detached process and returns at once. The driver:

1. opens the dedicated-profile browser (headed) and, per job, a tab with the submit guard on;
2. fills it through the ATS adapter, reads every value back, highlights what needs the human,
   takes a screenshot, and marks the job **Prefilled**;
3. releases the guard (handoff) only after it has stopped interacting with that tab;
4. watches the open tabs. When a tab shows the ATS's confirmation after the human submits, the
   job is marked **Applied** and its application record is created: the confirmation ID (when
   the ATS shows one), a screenshot of the confirmation, a snapshot of the job description and
   where to check its status. A tab closed without submitting stays Prefilled.

It never clicks Submit. Its state is a JSON file that ``jobhunt prefill --status`` reads; values
never go into it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from jobhunt.config import load_config
from jobhunt.locking import file_lock
from jobhunt.paths import Paths, get_paths, secure_file
from jobhunt.prefill.adapters import Adapter, adapter_for
from jobhunt.prefill.browser import persistent_context
from jobhunt.prefill.fixtures import FixtureRouter
from jobhunt.prefill.guard import RouteGuard
from jobhunt.prefill.review import TabReview, highlight, review, screenshot
from jobhunt.prefill.values import read_sheet
from jobhunt.procs import pid_alive

log = logging.getLogger("jobhunt.prefill")

ACTIVE = ("starting", "filling", "waiting_for_you")


class BatchJob(BaseModel):
    job_id: int
    company: str = ""
    title: str = ""
    ats: str
    url: str
    sheet: str = Field(description="Path to this job's answers.json.")


class Plan(BaseModel):
    batch_id: str
    created_at: str
    jobs: list[BatchJob]
    headless: bool = False
    fixtures: str | None = None
    parse_wait_s: float = 20
    watch_hours: float = 6


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_path(paths: Paths) -> Path:
    return paths.prefill / "batch.json"


def state_path(paths: Paths) -> Path:
    return paths.prefill / "state.json"


def _write_json(p: Path, data: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    secure_file(tmp)
    tmp.replace(p)


def read_state(paths: Paths) -> dict[str, Any] | None:
    try:
        return json.loads(state_path(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def running(paths: Paths) -> dict[str, Any] | None:
    st = read_state(paths)
    if st and st.get("status") in ACTIVE and pid_alive(st.get("pid")):
        return st
    return None


# --- store updates (each under the file lock, held only for the write) ---------------------------


def mark_prefilled(paths: Paths, job_id: int) -> bool:
    from jobhunt.store import open_store
    from jobhunt.tracker.store import agent_advance

    with file_lock(paths.lock, timeout=60), open_store(paths.db) as conn:
        return agent_advance(conn, job_id, "Prefilled", now_iso())


def mark_applied(
    paths: Paths,
    job_id: int,
    *,
    now: str | None = None,
    sheet: str | None = None,
    shot: str | None = None,
    confirmation_id: str | None = None,
) -> int:
    """The human submitted: record the application (Applied, its proof and where to check its
    status). Returns the application id."""
    from jobhunt.cli.onboarding import default_resume
    from jobhunt.store import open_store
    from jobhunt.tracker.applications import record_application, submitted_from_sheet
    from jobhunt.tracker.timefmt import zone
    from jobhunt.tracker.views import refresh_views

    cfg = load_config(paths)
    now = now or now_iso()
    tz = zone(cfg.tracking.timezone)
    answers = read_sheet(Path(sheet)) if sheet and Path(sheet).exists() else None
    with file_lock(paths.lock, timeout=60), open_store(paths.db) as conn:
        submitted = submitted_from_sheet(conn, answers) if answers else []
        resume = next(
            (Path(str(s.value)) for s in submitted if s.kind == "file" and s.value), None
        ) or default_resume(paths)
        out = record_application(
            conn, job_id, now=now, applied_at=now, approx=False, method="prefilled",
            via="prefill", tz=tz, paths=paths, window_days=cfg.volume_guard.window_days,
            max_per_company=cfg.volume_guard.max_applications_per_company, resume=resume,
            submitted=submitted, confirmation_id=confirmation_id,
            screenshot=Path(shot) if shot else None,
        )  # fmt: skip
        refresh_views(conn, paths, cfg)
        return out.app_id


# --- filling and watching ------------------------------------------------------------------------


@dataclass
class Tab:
    job: BatchJob
    adapter: Adapter
    page: Any = None
    guard: RouteGuard | None = None
    review: TabReview | None = None
    status: str = "filling"  # filling | ready_for_review | failed | applied | closed | stopped
    applied_on: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        out = {
            "job_id": self.job.job_id,
            "company": self.job.company,
            "title": self.job.title,
            "ats": self.job.ats,
            "status": self.status,
            "applied_on": self.applied_on,
        }
        if self.review:
            out["review"] = self.review.model_dump(mode="json")
        return out


def fill_tab(ctx: Any, tab: Tab, screens: Path) -> None:
    """Open, guard, fill, read back, highlight and screenshot one job; then hand off."""
    sheet = read_sheet(Path(tab.job.sheet))
    page = ctx.new_page()
    tab.page = page
    guard = tab.adapter.guard()
    tab.guard = guard
    guard.install(page)  # before the first request: nothing state-changing leaves the page
    try:
        page.goto(tab.job.url, wait_until="domcontentloaded", timeout=45_000)
        outcomes = tab.adapter.fill(page, sheet)
        highlight(page, tab.adapter, sheet, outcomes)
        shot = screenshot(page, screens, tab.job.job_id, now_iso())
        tab.review = review(
            tab.job.job_id,
            sheet,
            outcomes,
            company=tab.job.company,
            title=tab.job.title,
            blocked=len(guard.blocked),
            shot=shot,
        )
        tab.status = "ready_for_review"
    except Exception as e:
        log.warning("job %s: fill failed: %s", tab.job.job_id, e)
        tab.review = TabReview(
            job_id=tab.job.job_id, company=tab.job.company, title=tab.job.title,
            ats=tab.job.ats, status="failed", error=f"{type(e).__name__}: {e}"[:300],
            blocked_requests=len(guard.blocked),
        )  # fmt: skip
        tab.status = "failed"
    finally:
        # handoff: the engine has stopped interacting with this tab; the human may submit
        guard.release(page)
    for b in guard.blocked:
        log.info("job %s: blocked during fill: %s %s", tab.job.job_id, b["method"], b["url"])


def record_confirmed(paths: Paths, tab: Tab, now: str | None = None) -> None:
    """The tab shows the confirmation: capture it, then record the application."""
    now = now or now_iso()
    tab.applied_on = now[:10]
    shot = confirmation_id = None
    with contextlib.suppress(Exception):  # proof is best effort; the record is not
        shot = screenshot(tab.page, paths.screens, tab.job.job_id, now)
        confirmation_id = tab.adapter.confirmation_id(tab.page)
    try:
        tab.extra["app_id"] = mark_applied(
            paths, tab.job.job_id, now=now, sheet=tab.job.sheet, shot=shot,
            confirmation_id=confirmation_id,
        )  # fmt: skip
    except Exception as e:
        log.warning("job %s: couldn't record the application: %s", tab.job.job_id, e)
        tab.extra["mark_error"] = str(e)


def check_tab(tab: Tab, on_applied: Callable[[Tab], None]) -> bool:
    """One look at a handed-off tab. True once it's resolved (applied or closed)."""
    if tab.status in ("applied", "closed", "stopped"):
        return True
    if tab.page is None or tab.page.is_closed():
        tab.status = "closed"  # closed without submitting: stays Prefilled
        return True
    try:
        if tab.adapter.confirmed(tab.page):
            tab.status = "applied"
            on_applied(tab)
            return True
    except Exception:  # navigating or closing right now; look again next round
        return False
    return False


def watch(
    tabs: list[Tab],
    on_applied: Callable[[Tab], None],
    on_change: Callable[[], None],
    *,
    deadline: float,
    should_stop: Callable[[], bool],
    poll_s: float = 1.0,
) -> None:
    pending = [t for t in tabs if t.status in ("ready_for_review", "failed")]
    while pending and time.monotonic() < deadline and not should_stop():
        changed = False
        for t in list(pending):
            before = t.status
            if check_tab(t, on_applied):
                pending.remove(t)
            changed |= t.status != before
        if changed:
            on_change()
        live = next((t.page for t in pending if t.page and not t.page.is_closed()), None)
        if live is None:
            time.sleep(poll_s)
            continue
        try:
            live.wait_for_timeout(poll_s * 1000)  # keeps Playwright's event loop turning
        except Exception:
            time.sleep(poll_s)
    for t in pending:
        if t.status not in ("applied", "closed"):
            t.status = "stopped"


# --- the process ---------------------------------------------------------------------------------


class Driver:
    def __init__(self, paths: Paths, plan: Plan) -> None:
        self.paths = paths
        self.plan = plan
        self.tabs: list[Tab] = []
        self.stop_requested = False
        self.state: dict[str, Any] = {
            "pid": os.getpid(),
            "batch_id": plan.batch_id,
            "status": "starting",
            "started_at": now_iso(),
            "tabs": [],
        }

    def save(self, **kw: Any) -> None:
        self.state.update(kw)
        self.state["tabs"] = [t.public() for t in self.tabs]
        self.state["updated_at"] = now_iso()
        _write_json(state_path(self.paths), self.state)

    def applied(self, tab: Tab) -> None:
        record_confirmed(self.paths, tab)

    def run_in(self, ctx: Any) -> None:
        if self.plan.fixtures:
            FixtureRouter(
                Path(self.plan.fixtures), log_path=self.paths.prefill / "fixture-requests.jsonl"
            ).install(ctx)
        self.tabs = [
            Tab(job=j, adapter=adapter_for(j.ats, parse_wait_s=self.plan.parse_wait_s))
            for j in self.plan.jobs
        ]
        self.save(status="filling")
        for tab in self.tabs:
            if self.stop_requested:
                break
            fill_tab(ctx, tab, self.paths.screens)
            if tab.status == "ready_for_review":
                try:
                    mark_prefilled(self.paths, tab.job.job_id)
                except Exception as e:
                    log.warning("job %s: couldn't mark Prefilled: %s", tab.job.job_id, e)
            self.save()
        self.save(status="waiting_for_you")
        watch(
            self.tabs,
            self.applied,
            self.save,
            deadline=time.monotonic() + self.plan.watch_hours * 3600,
            should_stop=lambda: self.stop_requested,
        )

    def run(self) -> None:
        cfg = load_config(self.paths)
        try:
            with persistent_context(self.paths, cfg.prefill, headless=self.plan.headless) as ctx:
                self.run_in(ctx)
        except Exception as e:
            if not self.stop_requested:
                log.warning("driver stopped: %s", e)
                for t in self.tabs:
                    if t.status in ("ready_for_review", "failed"):
                        t.status = "closed"  # the browser went away without a submission
        self.save(status="stopped" if self.stop_requested else "done", ended_at=now_iso())


def launch(paths: Paths, plan: Plan, *, wait_s: float = 15.0) -> dict[str, Any]:
    """Start the driver detached; return its state once it is up."""
    _write_json(plan_path(paths), plan.model_dump(mode="json"))
    state_path(paths).unlink(missing_ok=True)
    paths.logs.mkdir(parents=True, exist_ok=True)
    logf = open(paths.logs / "prefill.log", "ab")  # noqa: SIM115 - handed to the child process
    env = {**os.environ, "JOBHUNT_HOME": str(paths.home)}
    kwargs: dict[str, Any] = {
        "stdout": logf, "stderr": logf, "stdin": subprocess.DEVNULL, "env": env,
    }  # fmt: skip
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([sys.executable, "-m", "jobhunt.prefill.driver"], **kwargs)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        st = read_state(paths)
        if st and st.get("pid") == proc.pid:
            return st
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    raise RuntimeError(f"The pre-fill browser didn't start; see {paths.logs / 'prefill.log'}")


def stop(paths: Paths) -> dict[str, Any] | None:
    st = read_state(paths)
    if not st:
        return None
    if pid_alive(st.get("pid")) and st.get("status") in ACTIVE:
        with contextlib.suppress(OSError):
            os.kill(int(st["pid"]), signal.SIGTERM)
        for _ in range(100):
            if not pid_alive(st.get("pid")):
                break
            time.sleep(0.1)
    return read_state(paths) or st


def main() -> None:
    ap = argparse.ArgumentParser(description="jobhunt pre-fill driver (`jobhunt prefill` runs it)")
    ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    paths = get_paths()
    plan = Plan.model_validate_json(plan_path(paths).read_text(encoding="utf-8"))
    driver = Driver(paths, plan)

    def on_term(signum: int, frame: Any) -> None:
        driver.stop_requested = True

    signal.signal(signal.SIGTERM, on_term)
    driver.run()


if __name__ == "__main__":
    main()
