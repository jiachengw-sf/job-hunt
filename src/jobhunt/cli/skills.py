"""``jobhunt skills install | status | remove``."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

import click
from pydantic import BaseModel

from jobhunt import skills as sk
from jobhunt.cli.framework import Result, agent_command
from jobhunt.doctor import detect_ai_tools
from jobhunt.errors import NeedsUser, ValidationFailed

TOOL = click.option(
    "--tool",
    type=click.Choice(["auto", "claude-code", "codex", "all"]),
    default="auto",
    help="Which AI tool (auto: every one detected on this machine).",
)
SCOPE = click.option(
    "--scope",
    type=click.Choice(["user", "project"]),
    default="user",
    help="user: ~/.claude/skills and ~/.agents/skills. project: .claude/skills and "
    ".agents/skills under --project-dir.",
)
PROJECT = click.option(
    "--project-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    help="Project root for --scope project (default: current directory).",
)


class SkillsReport(BaseModel):
    dry_run: bool = False
    tools: list[dict[str, Any]]


def _tools(tool: str) -> list[sk.Tool]:
    if tool == "all":
        return ["claude-code", "codex"]
    if tool != "auto":
        return [tool]  # type: ignore[list-item]
    found = list(detect_ai_tools())
    if not found:
        raise NeedsUser(
            "No supported AI tool found (Claude Code or Codex CLI).",
            user_actions=["Install Claude Code or Codex CLI, or pass --tool claude-code|codex."],
        )
    return found  # type: ignore[return-value]


def register(cli: click.Group) -> None:
    @agent_command(cli, "skills install", output=SkillsReport, writes=True)
    @TOOL
    @SCOPE
    @PROJECT
    @click.option(
        "--mode",
        type=click.Choice(["copy", "symlink"]),
        default="copy",
        help="copy (default) or symlink to the shipped skills (for development). The "
        "explicit-only apply skill is always copied, since its setting is tool-specific.",
    )
    @click.option("--force", is_flag=True, help="Replace same-named skills jobhunt didn't install.")
    def install_cmd(
        tool: str, scope: str, project_dir: Path, mode: str, force: bool, dry_run: bool
    ) -> Result:
        """Install the seven jobhunt skills for Claude Code and/or Codex. Safe to re-run.

        jobhunt-apply is explicit-only: Claude Code's copy gets `disable-model-invocation:
        true`; Codex's copy gets agents/openai.yaml with `allow_implicit_invocation: false`.
        """
        out = []
        for t in _tools(tool):
            dest = sk.target_dir(t, scope, project_dir.resolve())  # type: ignore[arg-type]
            o = sk.install(t, dest, mode=mode, force=force, dry_run=dry_run)  # type: ignore[arg-type]
            out.append(asdict(o))
        user = []
        if any(o["skipped"] for o in out):
            user.append("Some skill folders belong to you, not jobhunt; they were left alone "
                        "(re-run with --force to replace them).")  # fmt: skip
        hint = {
            "claude-code": "Claude Code: start a new session; apply runs only via /jobhunt-apply.",
            "codex": "Codex: start a new session; apply runs only via $jobhunt-apply.",
        }  # fmt: skip
        return Result(
            data=SkillsReport(dry_run=dry_run, tools=out),
            next_actions=[hint[o["tool"]] for o in out],
            user_actions=user,
        )

    @agent_command(cli, "skills status", output=SkillsReport)
    @TOOL
    @SCOPE
    @PROJECT
    def status_cmd(tool: str, scope: str, project_dir: Path) -> Result:
        """Which jobhunt skills are installed for each tool, and is apply explicit-only?"""
        out = []
        for t in _tools(tool):
            dest = sk.target_dir(t, scope, project_dir.resolve())  # type: ignore[arg-type]
            names = sk.skill_names()
            present = [n for n in names if (dest / n / "SKILL.md").is_file()]
            out.append({
                "tool": t, "directory": str(dest), "installed": present,
                "missing": [n for n in names if n not in present],
                "explicit_only_ok": {n: sk.explicit_ok(t, dest / n)
                                     for n in sk.EXPLICIT_ONLY if n in present},
            })  # fmt: skip
        bad = [o["tool"] for o in out if o["missing"] or not all(o["explicit_only_ok"].values())]
        return Result(
            data=SkillsReport(tools=out),
            next_actions=["Run `jobhunt skills install`."] if bad else [],
        )

    @agent_command(cli, "skills remove", output=SkillsReport, writes=True)
    @TOOL
    @SCOPE
    @PROJECT
    def remove_cmd(tool: str, scope: str, project_dir: Path, dry_run: bool) -> Result:
        """Remove the skills jobhunt installed (never ones it didn't)."""
        if tool == "auto":
            raise ValidationFailed("Name the tool to remove from: --tool claude-code|codex|all.")
        out = []
        for t in _tools(tool):
            dest = sk.target_dir(t, scope, project_dir.resolve())  # type: ignore[arg-type]
            out.append({"tool": t, "directory": str(dest),
                        "removed": sk.remove(dest, dry_run=dry_run)})  # fmt: skip
        return Result(data=SkillsReport(dry_run=dry_run, tools=out))
