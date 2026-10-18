"""``jobhunt research``: a cached company brief, or a budgeted research task for the AI."""

from __future__ import annotations

from typing import Any, Literal

import click
from pydantic import BaseModel

from jobhunt.cli.discovery import _prepare
from jobhunt.cli.fit import _inputs, _lock
from jobhunt.cli.framework import Result, agent_command
from jobhunt.discovery.text import utc_now
from jobhunt.errors import NotFound, ValidationFailed
from jobhunt.fit.profile_lines import profile_lines
from jobhunt.research import cached_brief, canonical_company, write_research_task
from jobhunt.store import open_store


class ResearchReport(BaseModel):
    company: str
    status: Literal["cached", "written", "pending"]
    brief: dict[str, Any] | None = None
    task_id: str | None = None
    task_path: str | None = None
    result_path: str | None = None
    budget: dict[str, int] | None = None


def register(cli: click.Group) -> None:
    @agent_command(cli, "research", output=ResearchReport, writes=True)
    @click.argument("company", required=False)
    @click.option("--job", "job_id", type=int, default=None, help="Research this job's company.")
    @click.option("--refresh", is_flag=True, help="Ignore a cached brief and write a new task.")
    def research_cmd(
        company: str | None, job_id: int | None, refresh: bool, dry_run: bool
    ) -> Result:
        """Show a company's cached brief, or write a research task for the jobhunt-research skill.

        Briefs are cached for research.cache_days (14). A task carries a hard budget (5 searches,
        8 page fetches, no subagents); every fact needs a URL and a quote of 25 words or fewer,
        which `jobhunt ingest` re-checks on the page.
        """
        paths, cfg = _prepare(dry_run)
        now = utc_now()
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            if job_id is not None:
                row = conn.execute("SELECT company FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row is None:
                    raise NotFound(f"No job {job_id}.", next_actions=["Run `jobhunt jobs`."])
                company = row["company"]
            if not company or not company.strip():
                raise ValidationFailed("Name a company or pass --job <id>.")
            name = canonical_company(conn, company)
            brief = None if refresh else cached_brief(conn, name, cfg.research, now)
            if brief is not None:
                return Result(data=ResearchReport(company=name, status="cached", brief=brief))
            lines = profile_lines(_inputs(paths, cfg, now).profile)
            status, task_id, path = write_research_task(
                conn,
                paths.tasks,
                paths.results,
                name,
                lines,
                cfg.research,
                now=now,
                dry_run=dry_run,
            )
        result_path = paths.results / f"{task_id}.json"
        return Result(
            data=ResearchReport(
                company=name,
                status=status,
                task_id=task_id,
                task_path=str(path),
                result_path=str(result_path),
                budget={
                    "max_searches": cfg.research.max_searches,
                    "max_fetches": cfg.research.max_fetches,
                },
            ),
            next_actions=[
                f"Do the research task in {path} (jobhunt-research skill), write {result_path}, "
                "then run `jobhunt ingest`."
            ],
        )
