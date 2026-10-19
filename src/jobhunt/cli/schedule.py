"""``jobhunt schedule install | status | remove``."""

from __future__ import annotations

from typing import Any

import click
from pydantic import BaseModel

from jobhunt import schedule as sch
from jobhunt.cli.discovery import _prepare
from jobhunt.cli.framework import Result, agent_command
from jobhunt.config import load_config
from jobhunt.paths import get_paths

PLATFORM = click.option(
    "--platform",
    type=click.Choice(["auto", "launchd", "cron", "windows"]),
    default="auto",
    help="Scheduler to use (auto: launchd on macOS, Task Scheduler on Windows, else cron).",
)


class ScheduleReport(BaseModel):
    scheduler: str
    action: str
    dry_run: bool = False
    interval_hours: float | None = None
    location: str | None = None
    entry: str | None = None
    status: dict[str, Any] | None = None


def _kind(platform: str) -> sch.Kind:
    return sch.detect() if platform == "auto" else platform  # type: ignore[return-value]


def register(cli: click.Group) -> None:
    @agent_command(cli, "schedule install", output=ScheduleReport, writes=True)
    @PLATFORM
    @click.option("--every", "hours", type=float, default=None,
                  help="Hours between runs (default: schedule.interval_hours, 4).")  # fmt: skip
    def install_cmd(platform: str, hours: float | None, dry_run: bool) -> Result:
        """Run `jobhunt run` on a schedule (default every 4 hours) with your OS's scheduler.

        Replaces an earlier jobhunt entry; touches nothing else. Logs go to ~/.jobhunt/logs.
        """
        paths, cfg = _prepare(dry_run)
        kind = _kind(platform)
        every = hours or cfg.schedule.interval_hours
        p = sch.plan(kind, paths, every) if dry_run else sch.install(kind, paths, every)
        return Result(
            data=ScheduleReport(scheduler=kind, action="install", dry_run=dry_run,
                                interval_hours=every, location=p.location, entry=p.entry),
            next_actions=["Check it with `jobhunt schedule status`."],
        )  # fmt: skip

    @agent_command(cli, "schedule status", output=ScheduleReport)
    @PLATFORM
    def status_cmd(platform: str) -> Result:
        """Is the scheduled run installed, and how often does it run?"""
        paths = get_paths()
        kind = _kind(platform)
        st = sch.status(kind)
        nxt = [] if st.get("installed") else ["Install it with `jobhunt schedule install`."]
        return Result(
            data=ScheduleReport(scheduler=kind, action="status", status=st,
                                interval_hours=load_config(paths).schedule.interval_hours),
            next_actions=nxt,
        )  # fmt: skip

    @agent_command(cli, "schedule remove", output=ScheduleReport, writes=True)
    @PLATFORM
    def remove_cmd(platform: str, dry_run: bool) -> Result:
        """Remove the scheduled run (only jobhunt's own entry)."""
        kind = _kind(platform)
        if dry_run:
            return Result(data=ScheduleReport(scheduler=kind, action="remove", dry_run=True,
                                              status=sch.status(kind)))  # fmt: skip
        had = sch.remove(kind)
        return Result(
            data=ScheduleReport(scheduler=kind, action="removed" if had else "nothing_to_remove")
        )
