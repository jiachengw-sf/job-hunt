"""Discovery commands: ``fetch`` and ``add``."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

import click

from jobhunt.cli.framework import Result, agent_command
from jobhunt.config import load_config, load_user_file, load_watchlist, require_onboarding
from jobhunt.discovery.pipeline import (
    MANUAL_ID,
    AddReport,
    FetchReport,
    add_url,
    plan_sources,
    run_fetch,
)
from jobhunt.discovery.text import utc_now
from jobhunt.envelope import Envelope, ErrorInfo, render_human_default
from jobhunt.errors import ExitCode, ValidationFailed
from jobhunt.http import FixtureTransport, HttpClient
from jobhunt.locking import file_lock
from jobhunt.models import Config, Preferences
from jobhunt.paths import Paths, get_paths
from jobhunt.store import open_store

FIXTURES_HELP = "Read sources from this fixture directory instead of the network (tests, demos)."


def make_client(cfg: Config, fixtures: Path | None) -> HttpClient:
    if fixtures is not None:
        if not fixtures.is_dir():
            raise ValidationFailed(f"--fixtures {fixtures} is not a directory.")
        return HttpClient(
            user_agent=cfg.http.user_agent,
            min_host_interval=0,
            max_retries=0,
            transport=FixtureTransport(fixtures),
        )
    return HttpClient(
        user_agent=cfg.http.user_agent,
        min_host_interval=cfg.http.min_host_interval_seconds,
        timeout=cfg.http.timeout_seconds,
        max_retries=cfg.http.max_retries,
    )


def _prepare(dry_run: bool) -> tuple[Paths, Config]:
    paths = get_paths()
    require_onboarding(paths)
    cfg = load_config(paths)
    if not dry_run:
        paths.ensure()
    return paths, cfg


def _render_fetch(env: Envelope) -> str:
    d = env.data
    if not isinstance(d, dict) or "sources" not in d:
        return render_human_default(env)
    lines = ["Source                                  status        fetched  new  chg  closed  err"]
    for s in d["sources"]:
        tag = " (baseline)" if s["baseline"] else ""
        lines.append(
            f"{s['source_id'][:38]:<40}{s['status']:<14}{s['fetched']:>7}{s['new']:>5}"
            f"{s['changed']:>5}{s['closed']:>8}{s['errors']:>5}{tag}"
        )
    t = d["totals"]
    lines.append(
        f"\n{t['fetched']} fetched, {t['new']} new, {t['changed']} changed, {t['closed']} closed, "
        f"{t['reopened']} reopened"
    )
    if d["new_jobs"]:
        lines.append(f"\nNew jobs ({d['new_jobs_total']}):")
        for j in d["new_jobs"]:
            sig = f"  [signals: {', '.join(j['signals'])}]" if j["signals"] else ""
            lines.append(f"  #{j['id']} {j['company']}: {j['title']}{sig}")
    if d["new_signals"]:
        lines.append("\nNew signals: " + ", ".join(f"{k} {v}" for k, v in d["new_signals"].items()))
    if d["coverage"]:
        cov = ", ".join(
            f"{k} {v['live_jobs']}{'' if v['supported'] else '*'}" for k, v in d["coverage"].items()
        )
        lines.append(f"\nLive jobs by ATS (* = not readable yet): {cov}")
    for w in d["warnings"]:
        lines.append(f"WARN {w}")
    if env.hints.user_actions:
        lines.append("\nYou need to:")
        lines += [f"  - {a}" for a in env.hints.user_actions]
    return "\n".join(lines)


def register(cli: click.Group) -> None:
    @agent_command(cli, "fetch", output=FetchReport, writes=True, render=_render_fetch)
    @click.option("--source", "only", multiple=True, help="Only fetch this source id (repeatable).")
    @click.option(
        "--fixtures",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help=FIXTURES_HELP,
    )
    def fetch_cmd(only: tuple[str, ...], fixtures: Path | None, dry_run: bool) -> Result:
        """Fetch new postings from every source, dedupe them, and log yields and closures.

        Sources: Greenhouse, Lever and Ashby boards in watchlist.yaml, jobs added with
        `jobhunt add`, and the SimplifyJobs listing that matches your preferences. A source's
        first fetch is a silent baseline. Re-running is safe: nothing is duplicated.
        """
        paths, cfg = _prepare(dry_run)
        prefs = load_user_file("preferences", paths)
        assert isinstance(prefs, Preferences)
        plan = plan_sources(cfg.discovery, prefs, load_watchlist(paths))
        known = {s.id for s in plan.sources} | {MANUAL_ID}
        if unknown := sorted(set(only) - known):
            raise ValidationFailed(
                f"Unknown source id(s): {', '.join(unknown)}.", details={"sources": sorted(known)}
            )
        client = make_client(cfg, fixtures)
        lock = nullcontext() if dry_run else file_lock(paths.lock)
        with lock, open_store(paths.db, dry_run=dry_run) as conn:
            report, raised, status = run_fetch(
                conn,
                client,
                plan,
                cfg=cfg.discovery,
                now=utc_now(),
                only=set(only) or None,
                dry_run=dry_run,
            )
        user_actions = []
        if ai := raised.get("ai_instructions"):
            user_actions.append(
                f"{len(ai)} new posting(s) contain text aimed at AI tools (job ids "
                f"{', '.join(map(str, ai[:10]))}). jobhunt flagged them and will not follow that "
                "text; read those postings yourself."
            )
        next_actions = []
        if not report.sources:
            next_actions.append(
                "No sources to fetch: add Greenhouse, Lever or Ashby boards to watchlist.yaml, or "
                "set roles.seniority in preferences.yaml to use a SimplifyJobs listing."
            )
        if status == "failed":
            return Result(
                data=report,
                ok=False,
                exit_code=ExitCode.NETWORK,
                error=ErrorInfo(code="sources_failed", message="Every source failed to fetch."),
                next_actions=["Check your network connection, then re-run `jobhunt fetch`."],
            )
        return Result(data=report, next_actions=next_actions, user_actions=user_actions)

    @agent_command(cli, "add", output=AddReport, writes=True)
    @click.argument("url")
    @click.option(
        "--company", default=None, help="Company name (needed when the ATS isn't readable)."
    )
    @click.option("--title", default=None, help="Job title (needed when the ATS isn't readable).")
    @click.option(
        "--fixtures",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help=FIXTURES_HELP,
    )
    def add_cmd(
        url: str, company: str | None, title: str | None, fixtures: Path | None, dry_run: bool
    ) -> Result:
        """Add one posting by URL (any ATS). Adding the same posting twice changes nothing.

        Greenhouse, Lever and Ashby postings are read from the ATS and re-checked on every
        fetch. For other ATSs pass --company and --title as shown on the posting.
        """
        if not url.lower().startswith(("http://", "https://")):
            raise ValidationFailed(f"Not a URL: {url!r}")
        paths, cfg = _prepare(dry_run)
        client = make_client(cfg, fixtures)
        lock = nullcontext() if dry_run else file_lock(paths.lock)
        with lock, open_store(paths.db, dry_run=dry_run) as conn:
            report = add_url(
                conn,
                client,
                url,
                cfg=cfg.discovery,
                now=utc_now(),
                company=company,
                title=title,
                dry_run=dry_run,
            )
        user_actions = []
        if any(s.kind == "ai_instructions" for s in report.signals):
            user_actions.append(
                "This posting contains text aimed at AI tools. jobhunt flagged it and will not "
                "follow it; read the posting yourself."
            )
        return Result(data=report, user_actions=user_actions)
