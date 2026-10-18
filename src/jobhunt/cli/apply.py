"""Application commands: inspect (read and classify forms), coverage, packet.

No command here, or anywhere, submits an application.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import click
from pydantic import BaseModel, Field

from jobhunt.bank.keys import BUILTIN, AnswerContext
from jobhunt.cli.discovery import FIXTURES_HELP, _prepare, make_client
from jobhunt.cli.framework import Result, agent_command
from jobhunt.cli.onboarding import default_resume
from jobhunt.discovery.text import utc_now
from jobhunt.drafts.essays import approved_essays
from jobhunt.envelope import Envelope, render_human_default
from jobhunt.errors import NotFound
from jobhunt.fit.jobview import load_jobs
from jobhunt.forms import ashby
from jobhunt.forms.classify import Classification, FieldResult, classify, display_value
from jobhunt.forms.coverage import CoverageReport, report, sample_jobs
from jobhunt.forms.inspect import (
    Inputs,
    InspectReport,
    inspect_jobs,
    null_loader_factory,
    reclassify_cached,
    unanswered_groups,
    write_new_questions,
)
from jobhunt.forms.model import FormSpec
from jobhunt.forms.packet import Packet, PacketAnswer, render
from jobhunt.http import HttpClient
from jobhunt.models import Config
from jobhunt.paths import Paths, secure_file
from jobhunt.store import open_store

SHOWN = 30


def form_inputs(paths: Paths, cfg: Config, now: str) -> Inputs:
    from jobhunt.cli.fit import _inputs

    fi = _inputs(paths, cfg, now)
    return Inputs(
        fi.profile,
        fi.answers,
        fi.prefs,
        default_resume(paths),
        now,
        threshold=cfg.forms.candidate_threshold,
    )


def loader_factory(cfg: Config, client: HttpClient, fixtures: Path | None):
    if fixtures is not None:
        return lambda: ashby.fixture_session(client)
    if not cfg.forms.ashby_browser:
        return null_loader_factory
    return lambda: ashby.browser_session()


def threshold_jobs(conn: sqlite3.Connection, threshold: float, limit: int) -> list[int]:
    rows = conn.execute(
        "SELECT j.id FROM jobs j JOIN fits f ON f.job_id=j.id WHERE j.live=1 AND f.recalled=1 "
        "AND coalesce(f.gate_status,'') != 'failed' AND coalesce(f.priority,0) >= ? "
        "ORDER BY f.priority DESC, j.id LIMIT ?",
        (threshold, limit),
    ).fetchall()
    return [r["id"] for r in rows]


def run_inspect(
    conn: sqlite3.Connection,
    ids: list[int],
    inp: Inputs,
    client: HttpClient,
    factory: Any,
    *,
    paths: Paths,
    cfg: Config,
    refresh: bool,
    dry_run: bool,
) -> InspectReport:
    jobs, stats = inspect_jobs(
        conn,
        ids,
        inp,
        client,
        ashby_loader_factory=factory,
        refresh=refresh,
        max_age_days=cfg.forms.spec_max_age_days,
    )
    reclassify_cached(conn, inp, set(ids))
    groups = unanswered_groups(conn)
    if not dry_run:
        write_new_questions(paths.new_questions, groups, inp.now)
    by_class: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for j in jobs:
        by_class[j.cls] = by_class.get(j.cls, 0) + 1
        by_status[j.status] = by_status.get(j.status, 0) + 1
    warnings = []
    if by_status.get("needs_browser"):
        warnings.append(
            f"{by_status['needs_browser']} Ashby form(s) need the browser extra: "
            "`uv tool install 'jobhunt[browser]'`, then `jobhunt doctor --fix`."
        )
    return InspectReport(
        dry_run=dry_run,
        inspected=len(jobs),
        by_class=by_class,
        by_status=by_status,
        fetched=stats["fetched"],
        cached=stats["cached"],
        descriptions_stored=stats["descriptions"],
        new_questions=len([g for g in groups if g["required_in"]]),
        jobs=jobs[:SHOWN],
        warnings=warnings,
    )


def _rematch(conn: sqlite3.Connection, paths: Paths, cfg: Config, now: str, dry_run: bool) -> None:
    """New posting text arrived: recompute requirements, bands and priorities."""
    from jobhunt.cli.fit import _inputs
    from jobhunt.fit.engine import run_match
    from jobhunt.fit.recall import make_embedder

    embedder, note = make_embedder(cfg.fit)
    run_match(
        conn,
        _inputs(paths, cfg, now),
        tasks_dir=paths.tasks,
        results_dir=paths.results,
        embedder=embedder,
        embed_note=note,
        dry_run=dry_run,
    )


def inspect_actions(rep: InspectReport) -> tuple[list[str], list[str]]:
    nxt, user = [], []
    if rep.by_class.get("needs_answers") or rep.new_questions:
        nxt.append(
            f"{rep.new_questions} unanswered question(s) across your jobs: run "
            "`jobhunt answers questions`, confirm suggested matches with `jobhunt answers "
            "confirm`, and send sensitive ones to the form (`jobhunt setup form`)."
        )
    if rep.by_class.get("essay"):
        nxt.append(f"{rep.by_class['essay']} job(s) need essays; the essay skill drafts them.")
    if rep.by_class.get("manual"):
        nxt.append(
            f"{rep.by_class['manual']} job(s) are Manual: `jobhunt packet <job>` makes a "
            "hand-off page."
        )
    return nxt, user


def _render_coverage(env: Envelope) -> str:
    d = env.data
    if not isinstance(d, dict) or "ats_mix" not in d:
        return render_human_default(env)
    cov = "n/a" if d["coverage"] is None else f"{round(d['coverage'] * 100)}%"
    lines = [
        f"Inspected {d['inspected']} matches ({d['readable']} forms read).",
        f"Required fields your answers cover: {cov} ({d['required_covered']} of "
        f"{d['required_fields']}), plus {d['essays']} required essay answer(s).",
        "Classes: " + ", ".join(f"{k} {v}" for k, v in sorted(d["by_class"].items())),
    ]
    if d["top_unanswered"]:
        lines.append("\nMost common unanswered questions:")
        for q in d["top_unanswered"]:
            tag = " [sensitive: answer in the form]" if q["sensitive"] else ""
            sug = f" (looks like {q['suggested_key']})" if q["suggested_key"] else ""
            lines.append(f"  {q['jobs']:>3} jobs  {q['question'][:90]}{sug}{tag}")
    lines.append("\nYour matches by ATS:")
    for m in d["ats_mix"]:
        lines.append(
            f"  {m['ats']:<16}{m['jobs']:>5}  {round(m['share'] * 100):>3}%  {m['handling']}"
        )
    if d.get("ats_note"):
        lines.append(d["ats_note"])
    return "\n".join(lines)


class PacketReport(BaseModel):
    job_id: int
    path: str
    url: str = Field(description="file:// URL of the local page.")
    cls: str | None = Field(None, alias="class")
    answers: int = Field(description="Answers ready to copy (values are only in the page).")
    missing: int
    essays: int

    model_config = {"populate_by_name": True}


def register(cli: click.Group) -> None:
    @agent_command(cli, "inspect", output=InspectReport, writes=True)
    @click.argument("job_ids", nargs=-1, type=int)
    @click.option("--top", type=int, default=30, help="How many top-priority jobs (no ids given).")
    @click.option("--refresh", is_flag=True, help="Re-read forms even if cached.")
    @click.option("--no-match", is_flag=True, help="Don't recompute fit after new posting text.")
    @click.option(
        "--fixtures",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help=FIXTURES_HELP,
    )
    def inspect_cmd(
        job_ids: tuple[int, ...],
        top: int,
        refresh: bool,
        no_match: bool,
        fixtures: Path | None,
        dry_run: bool,
    ) -> Result:
        """Read and classify application forms: Ready, Needs answers, Essay or Manual.

        Default: recalled jobs at or above fit.priority_threshold. Greenhouse via its questions
        API, Lever via the /apply page, Ashby via a headless read-only page load. Never submits.
        Posting text read along the way fills in jobs whose source had none (Simplify).
        """
        paths, cfg = _prepare(dry_run)
        now = utc_now()
        inp = form_inputs(paths, cfg, now)
        client = make_client(cfg, fixtures)
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            ids = list(job_ids) or threshold_jobs(conn, cfg.fit.priority_threshold, top)
            if job_ids:
                known = {j.id for j in load_jobs(conn, ids, live_only=False)}
                if missing := [i for i in ids if i not in known]:
                    raise NotFound(f"No job(s) {missing}.", next_actions=["Run `jobhunt jobs`."])
            rep = run_inspect(
                conn,
                ids,
                inp,
                client,
                loader_factory(cfg, client, fixtures),
                paths=paths,
                cfg=cfg,
                refresh=refresh,
                dry_run=dry_run,
            )
            if rep.descriptions_stored and not no_match:
                _rematch(conn, paths, cfg, now, dry_run)
        nxt, user = inspect_actions(rep)
        if not ids:
            nxt.insert(0, "No jobs at or above the priority threshold: run `jobhunt match` first.")
        return Result(data=rep, next_actions=nxt, user_actions=user)

    @agent_command(cli, "coverage", output=CoverageReport, writes=True, render=_render_coverage)
    @click.option("--sample", type=int, default=30, help="How many top matches to inspect.")
    @click.option("--refresh", is_flag=True, help="Re-read forms even if cached.")
    @click.option(
        "--fixtures",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help=FIXTURES_HELP,
    )
    def coverage_cmd(sample: int, refresh: bool, fixtures: Path | None, dry_run: bool) -> Result:
        """How much of your matches' application forms your answers cover, and what's missing.

        Inspects about 30 top matches; reports the share of required fields answered, the most
        common unanswered questions, classes, and pre-fill coverage by ATS.
        """
        paths, cfg = _prepare(dry_run)
        now = utc_now()
        inp = form_inputs(paths, cfg, now)
        client = make_client(cfg, fixtures)
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            ids = sample_jobs(conn, sample)
            rep = run_inspect(
                conn,
                ids,
                inp,
                client,
                loader_factory(cfg, client, fixtures),
                paths=paths,
                cfg=cfg,
                refresh=refresh,
                dry_run=dry_run,
            )
            cov = report(conn, ids, rep.jobs, dry_run=dry_run)
            if rep.descriptions_stored:
                _rematch(conn, paths, cfg, now, dry_run)
        cov.jobs = []
        nxt, user = inspect_actions(rep)
        if not ids:
            nxt.insert(0, "No recalled matches on a supported ATS yet: run `jobhunt run` first.")
        if any(q.sensitive for q in cov.top_unanswered):
            user.append(
                "Some common questions are sensitive: answer them in the local form "
                "(`jobhunt setup form`), not in chat."
            )
        return Result(data=cov, next_actions=nxt, user_actions=user)

    @agent_command(cli, "packet", output=PacketReport, writes=True)
    @click.argument("job_id", type=int)
    def packet_cmd(job_id: int, dry_run: bool) -> Result:
        """Write a local hand-off page for one job: apply link, fit, answers to copy, essays.

        For Manual jobs and anyone applying by hand. Your answer values appear only in that
        local page, never in this output.
        """
        from jobhunt.cli.fit import job_detail

        paths, cfg = _prepare(dry_run)
        now = utc_now()
        inp = form_inputs(paths, cfg, now)
        with open_store(paths.db, dry_run=True) as conn:
            detail = job_detail(conn, job_id)
            row = conn.execute("SELECT * FROM form_specs WHERE job_id=?", (job_id,)).fetchone()
            job = load_jobs(conn, [job_id], live_only=False)[0]
            approved = approved_essays(conn, job_id)
        ctx = inp.ctx(job)
        cls: Classification | None = None
        if row and row["spec_json"]:
            spec = FormSpec.model_validate_json(row["spec_json"])
            cls = classify(spec, ctx, inp.matcher, eeo_confirmed=inp.answers.eeo.confirmed)
        elif row and row["details_json"]:
            cls = Classification.model_validate(json.loads(row["details_json"]))
        form_answers, essays = [], []
        for fr in cls.fields if cls else []:
            if fr.resolution == "essay":
                essays.append(
                    PacketAnswer(
                        fr.label,
                        approved.get(fr.id),
                        note=fr.description,
                        max_length=fr.max_length,
                        required=fr.required,
                    )
                )
                continue
            form_answers.append(_packet_answer(fr, ctx, inp))
        common = _common_answers(ctx, inp)
        url = (row["url"] if row and row["url"] else None) or detail["job"]["url"]
        packet = Packet(
            job={**detail["job"], "url": url},
            fit=detail["fit"],
            cls=cls.cls if cls else (row["class"] if row else None),
            reasons=cls.reasons if cls else ([row["error"]] if row and row["error"] else []),
            form_answers=form_answers,
            common_answers=common,
            essays=essays,
            captcha_at_submit=bool(cls and cls.captcha_at_submit),
            requirements=detail["requirements"],
            gates=detail["gates"],
        )
        slug = re.sub(r"[^a-z0-9]+", "-", f"{job.company} {job.title}".lower()).strip("-")[:60]
        path = paths.packets / f"{job_id}-{slug}.html"
        if not dry_run:
            paths.packets.mkdir(parents=True, exist_ok=True)
            path.write_text(render(packet), encoding="utf-8")
            secure_file(path)
        answered = [a for a in form_answers if a.value] or [a for a in common if a.value]
        return Result(
            data=PacketReport(
                job_id=job_id,
                path=str(path),
                url=path.as_uri(),
                cls=packet.cls,
                answers=len(answered),
                missing=len([a for a in form_answers if not a.value and a.required]),
                essays=len(essays),
            ),
            user_actions=[
                f"Open {path.as_uri()} to apply by hand. When you've submitted, run "
                f"`jobhunt mark {job_id} Applied` (or set it in the tracker)."
            ],
        )


def _packet_answer(fr: FieldResult, ctx: AnswerContext, inp: Inputs) -> PacketAnswer:
    val = display_value(fr, ctx, inp.matcher)
    note = fr.reason
    if fr.resolution == "human_only":
        note = "yours to answer on the form"
    return PacketAnswer(
        fr.label, val, note=note, max_length=fr.max_length, required=fr.required, human=fr.human
    )


def _common_answers(ctx: AnswerContext, inp: Inputs) -> list[PacketAnswer]:
    out = []
    for key, spec in BUILTIN.items():
        if spec.kind in ("essay", "file") or key in ("identity.location",):
            continue
        if spec.eeo and not inp.answers.eeo.confirmed:
            continue
        v = spec.resolve(ctx)
        if v in (None, "", []):
            continue
        out.append(PacketAnswer(spec.question, ", ".join(v) if isinstance(v, list) else str(v)))
    for e in inp.answers.bank:
        if e.key not in BUILTIN and e.answer:
            out.append(PacketAnswer(e.question, e.answer))
    return out


def _lock(paths: Paths, dry_run: bool) -> AbstractContextManager[Any]:
    from jobhunt.cli.fit import _lock as fit_lock

    return fit_lock(paths, dry_run)
