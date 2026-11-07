"""One Workday application in the pre-fill browser, step by step.

The tab opens on the employer's sign-in page; the human signs in (jobhunt never does). From then
on, every time the tab shows a step jobhunt hasn't handled, :meth:`WorkdayRun.tick`:

1. installs the submit guard,
2. reads the step into a FormSpec (:meth:`Workday.read_step`),
3. classifies it, builds and validates that step's answers exactly as for any other ATS (values
   only from the profile and the answer bank; sensitive and EEO answers only from the user's own
   answers; a field that fails validation is left for the user),
4. fills, reads every value back, fills My Experience's repeatable sections from the profile,
   and highlights what needs the human,
5. clicks Save and Continue only when every required field on the step has a value, Workday
   shows no error, and nothing came back mismatched or unmatched. Otherwise it stops and waits:
   the human finishes the step and clicks Save and Continue, and the next step is picked up,
6. releases the guard.

It never acts on the Review step: reaching Review is "ready for review", and the human clicks
Submit. Step answer sheets are written owner-only next to the job's answers.json.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobhunt.prefill.adapters.base import FieldOutcome
from jobhunt.prefill.adapters.workday import Workday
from jobhunt.prefill.review import ReviewItem, TabReview, highlight, note_text, screenshot
from jobhunt.prefill.values import AnswerSheet, build_sheet, validate, write_sheet

log = logging.getLogger("jobhunt.prefill")

# Outcomes that stop the step from being saved: something went wrong, never a guess.
HARD = frozenset({"mismatch", "not_found", "no_option", "refused", "file_rejected", "error"})
STEP_BANNER = (
    "jobhunt filled this step and stopped: check the highlighted fields, finish them, then "
    "click Save and Continue yourself. jobhunt picks up the next step."
)


@dataclass
class StepLog:
    name: str
    filled: int = 0
    to_fill: int = 0
    needs_you: list[ReviewItem] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    advanced: bool = False


@dataclass
class Inputs:
    """What a step's answers are built from (loaded once per tab)."""

    ctx: Any
    matcher: Any
    eeo_confirmed: bool


def load_inputs(paths: Any, job_id: int) -> Inputs:
    from jobhunt.cli.apply import form_inputs
    from jobhunt.cli.onboarding import job_resume
    from jobhunt.config import load_config
    from jobhunt.discovery.text import utc_now
    from jobhunt.fit.jobview import load_jobs
    from jobhunt.store import open_store

    cfg = load_config(paths)
    inp = form_inputs(paths, cfg, utc_now())
    with open_store(paths.db, dry_run=True) as conn:
        job = load_jobs(conn, [job_id], live_only=False)[0]
    ctx = inp.ctx(job)
    ctx.resume, _ = job_resume(paths, job_id, kept_only=True)
    return Inputs(ctx=ctx, matcher=inp.matcher, eeo_confirmed=inp.answers.eeo.confirmed)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "step"


@dataclass
class WorkdayRun:
    job_id: int
    company: str
    title: str
    url: str
    sheet_dir: Path
    screens: Path
    inputs: Inputs
    adapter: Workday = field(default_factory=Workday)
    status: str = "waiting_sign_in"  # waiting_sign_in | filling | needs_you | ready_for_review
    steps: list[StepLog] = field(default_factory=list)
    handled: set[str] = field(default_factory=set)
    blocked: int = 0
    shot: str | None = None
    resume: str | None = None
    now: str = ""

    def tick(self, page: Any) -> bool:
        """One look at the tab. True when its status changed."""
        before = self.status
        name = self.adapter.step_name(page)
        if name and self.adapter.on_review(page):
            if self.status != "ready_for_review":
                self.shot = screenshot(page, self.screens, self.job_id, self.now or "review")
                self.status = "ready_for_review"
            return self.status != before
        if not name or not self.adapter.on_form(page):
            if self.status == "filling":
                self.status = "needs_you"
            if not self.steps:
                self.status = "waiting_sign_in"
            return self.status != before
        if name in self.handled:
            return False  # the human is on a step jobhunt already filled
        self.handled.add(name)
        self.status = "filling"
        try:
            self.fill_step(page, name)
        except Exception as e:  # a page that changed under us: hand it over, never guess
            log.warning("job %s: Workday step %r failed: %s", self.job_id, name, e)
            self.steps.append(StepLog(name=name, errors=[f"{type(e).__name__}: {str(e)[:200]}"]))
            self.status = "needs_you"
        return True

    def fill_step(self, page: Any, name: str) -> None:
        from jobhunt.forms.classify import classify

        ad = self.adapter
        guard = ad.guard()
        guard.install(page)
        step = StepLog(name=name)
        self.steps.append(step)
        try:
            ad.settle(page, 8_000)
            read = ad.read_step(page, self.url)
            inp = self.inputs
            cls = classify(read.spec, inp.ctx, inp.matcher, eeo_confirmed=inp.eeo_confirmed)
            sheet = build_sheet(self.job_id, read.spec, cls, inp.ctx, inp.matcher, self.now)
            for p in validate(sheet, read.spec):  # a bad value never reaches the page
                ff = next((f for f in sheet.fields if f.id == p.id), None)
                if ff is not None and ff.action in ("fill", "recommend"):
                    ff.action, ff.value, ff.note = "human", None, p.problem
            keep_page_values(sheet, ad)
            write_sheet(self.sheet_dir / f"workday-{slug(name)}.json", sheet)
            outcomes = ad.fill(page, sheet)
            if ad.entries(page, "workExperience") or ad.entries(page, "education"):
                outcomes += ad.fill_experience(page, inp.ctx)
            if any(f.key == "files.resume" and f.value for f in sheet.fields):
                self.resume = Path(str(next(f.value for f in sheet.fields
                                            if f.key == "files.resume"))).name  # fmt: skip
            c = ad.step_check(page)
            step.empty, step.errors = c["empty"], c["errors"]
            step.to_fill = len(sheet.to_fill())
            step.filled = sum(1 for o in outcomes if o.status == "filled")
            step.needs_you = items(outcomes)
            hard = [o for o in outcomes if o.status in HARD]
            if c["empty"] or c["errors"] or hard:
                highlight(page, ad, sheet, outcomes, banner=STEP_BANNER)
                self.shot = screenshot(page, self.screens, self.job_id, f"{self.now}-{slug(name)}")
                self.status = "needs_you"
                return
            nxt = ad.save_and_continue(page)
            step.advanced = nxt is not None
            if nxt is None:
                c = ad.step_check(page)
                step.empty, step.errors = c["empty"], c["errors"] or ["Workday didn't move on"]
                self.status = "needs_you"
            else:
                self.status = "filling"
        finally:
            self.blocked += len(guard.blocked)
            for b in guard.blocked:
                log.info("job %s: blocked during fill: %s %s", self.job_id, b["method"], b["url"])
            guard.release(page)

    def review(self) -> TabReview:
        needs: list[ReviewItem] = []
        for s in self.steps:
            needs += [i.model_copy(update={"label": f"{s.name}: {i.label}"}) for i in s.needs_you]
            needs += [ReviewItem(id=f"{slug(s.name)}/empty/{n}", label=f"{s.name}: {e}",
                                 required=True, status="left", note="required and still empty")
                      for n, e in enumerate(s.empty)]  # fmt: skip
        problems = [f"{s.name}: {e}" for s in self.steps for e in s.errors]
        return TabReview(
            job_id=self.job_id, company=self.company, title=self.title, ats="workday",
            status="ready_for_review" if self.status == "ready_for_review" or not problems
            else "failed",
            filled=sum(s.filled for s in self.steps), to_fill=sum(s.to_fill for s in self.steps),
            resume=self.resume, needs_you=needs, problems=problems,
            blocked_requests=self.blocked, screenshot=self.shot,
        )  # fmt: skip


def keep_page_values(sheet: AnswerSheet, ad: Workday) -> None:
    """A field the page already holds (Workday's default, the human's earlier draft) that jobhunt
    has no answer for is left as it is, not reported as missing."""
    for ff in sheet.fields:
        b = ad.boxes.get(ff.id)
        if b and b.has and ff.action in ("human", "skip") and not ff.human:
            ff.action, ff.note = "skip", "already set on the page: check it"


def items(outcomes: list[FieldOutcome]) -> list[ReviewItem]:
    out = [
        ReviewItem(
            id=o.id,
            label=o.label,
            required=o.required,
            status=o.status,
            note=note_text(o, values=False).removeprefix("jobhunt: ") or None,
        )
        for o in outcomes
        if o.needs_you
    ]
    out.sort(key=lambda i: (not i.required, i.status == "left"))
    return out
