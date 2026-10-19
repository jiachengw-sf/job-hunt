"""``jobhunt digest`` and ``jobhunt notify test``."""

from __future__ import annotations

import click
from pydantic import BaseModel, Field

from jobhunt.cli.answers import _new_questions
from jobhunt.cli.discovery import _prepare
from jobhunt.cli.fit import _lock
from jobhunt.cli.framework import Result, agent_command
from jobhunt.discovery.text import utc_now
from jobhunt.envelope import Envelope, render_human_default
from jobhunt.errors import ValidationFailed
from jobhunt.notify.channels import NotifyError, configured
from jobhunt.notify.digest import Digest, build_digest, last_digest, push, record
from jobhunt.store import open_store


class DigestReport(BaseModel):
    digest: Digest
    pushed: dict[str, str] = Field(default_factory=dict)
    dry_run: bool


class TestReport(BaseModel):
    results: dict[str, str]


def _render(env: Envelope) -> str:
    d = env.data
    if not isinstance(d, dict) or "digest" not in d:
        return render_human_default(env)
    return d["digest"]["text"]


def register(cli: click.Group) -> None:
    @agent_command(cli, "digest", output=DigestReport, writes=True, render=_render)
    @click.option("--since", default=None, help="Start here (ISO time) instead of the last digest.")
    @click.option("--push", "push_", is_flag=True, help="Also send it to ntfy / Discord / email.")
    def digest_cmd(since: str | None, push_: bool, dry_run: bool) -> Result:
        """What's new since the last digest and what's waiting on you. Marks it as seen.

        Ready jobs, questions only you can answer, Essay jobs with prompts and deadlines, Manual
        jobs, closures, legitimacy flags, follow-ups due and referral questions.
        """
        paths, cfg = _prepare(dry_run)
        now = utc_now()
        channels = configured(cfg.notifications, cfg.http.user_agent) if push_ else []
        if push_ and not channels:
            raise ValidationFailed(
                "No push channel is configured.",
                next_actions=["Set notifications.ntfy_topic, discord_webhook or smtp in "
                              "config.yaml (the jobhunt-setup skill walks through it)."],
            )  # fmt: skip
        with _lock(paths, dry_run), open_store(paths.db, dry_run=dry_run) as conn:
            d = build_digest(conn, cfg, _new_questions(paths), since=since or last_digest(conn),
                             now=now)  # fmt: skip
            pushed = (
                push(conn, channels, "digest", "jobhunt digest", d.text, now) if channels else {}
            )
            record(conn, "digest", "terminal", now)
        return Result(data=DigestReport(digest=d, pushed=pushed, dry_run=dry_run))

    @agent_command(cli, "notify test", output=TestReport, writes=True)
    def test_cmd(dry_run: bool) -> Result:
        """Send a short test message to every configured channel."""
        _paths, cfg = _prepare(dry_run)
        channels = configured(cfg.notifications, cfg.http.user_agent)
        if not channels:
            raise ValidationFailed("No push channel is configured (config.yaml notifications).")
        if dry_run:
            return Result(data=TestReport(results={c.name: "would send" for c in channels}))
        res: dict[str, str] = {}
        for ch in channels:  # a test is not a digest: nothing is recorded
            try:
                ch.send("jobhunt test", "If you can read this, jobhunt can reach you here.")
                res[ch.name] = "sent"
            except NotifyError as e:
                res[ch.name] = f"failed: {e}"
        failed = [k for k, v in res.items() if v != "sent"]
        return Result(
            data=TestReport(results=res),
            user_actions=[f"Check {', '.join(sorted(res))} for the test message."]
            + ([f"Fix {', '.join(failed)} in config.yaml."] if failed else []),
        )
