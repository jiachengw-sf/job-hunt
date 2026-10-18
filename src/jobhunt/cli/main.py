"""Entry point. Agent-facing: one JSON envelope on stdout, logs on stderr, no prompts.

There is deliberately no `submit` command, and there never will be. The human clicks Submit.
"""

from __future__ import annotations

import sys
from contextlib import nullcontext
from typing import Any

import click
from pydantic import BaseModel, Field

from jobhunt import __version__
from jobhunt.cli.answers import register as register_answers
from jobhunt.cli.apply import register as register_apply
from jobhunt.cli.discovery import register as register_discovery
from jobhunt.cli.essays import register as register_essays
from jobhunt.cli.fit import register as register_fit
from jobhunt.cli.framework import (
    REGISTRY,
    Result,
    agent_command,
    configure_logging,
    emit,
    error_envelope,
    input_schema,
    output_schema,
)
from jobhunt.cli.ingest import register as register_ingest
from jobhunt.cli.onboarding import register as register_onboarding
from jobhunt.cli.prefill import register as register_prefill
from jobhunt.cli.research import register as register_research
from jobhunt.doctor import DoctorReport, doctor
from jobhunt.envelope import Envelope, ErrorInfo
from jobhunt.errors import EXIT_CODE_MEANINGS, ExitCode, NotFound, ValidationFailed
from jobhunt.locking import file_lock
from jobhunt.paths import get_paths


@click.group(context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100})
@click.option("-v", "--verbose", count=True, help="More logging on stderr (-vv for debug).")
@click.version_option(__version__, prog_name="jobhunt")
def cli(verbose: int) -> None:
    """jobhunt: find fitting roles, pre-fill applications, and track it all. You click Submit."""
    configure_logging(verbose)


# --- doctor ---------------------------------------------------------------------------------


def _render_doctor(env: Envelope) -> str:
    if not isinstance(env.data, dict) or "checks" not in env.data:
        from jobhunt.envelope import render_human_default

        return render_human_default(env)
    icon = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "skip": "skip"}
    lines = []
    for c in env.data["checks"]:
        lines.append(f"[{icon[c['status']]}] {c['name']}: {c['summary']}")
        if c.get("fix") and c["status"] in ("warn", "fail"):
            lines.append(f"        fix: {c['fix']}")
    if env.data.get("fixed"):
        verb = "Would apply" if env.data.get("dry_run") else "Applied"
        lines.append(f"\n{verb}:")
        lines += [f"  - {f}" for f in env.data["fixed"]]
    n = env.data["counts"]
    lines.append(f"\n{n['ok']} ok, {n['warn']} warnings, {n['fail']} failing, {n['skip']} skipped")
    return "\n".join(lines)


@agent_command(cli, "doctor", output=DoctorReport, writes=True, render=_render_doctor)
@click.option(
    "--fix", is_flag=True, help="Apply safe fixes: create dirs, starter config, migrate DB."
)
@click.option("--offline", is_flag=True, help="Skip network reachability checks.")
def doctor_cmd(fix: bool, offline: bool, dry_run: bool) -> Result:
    """Check the environment and print the exact fix for every problem.

    Exit 0 when nothing fails (warnings are fine), 9 when a check fails.
    """
    paths = get_paths()
    writing = fix and not dry_run
    if writing:
        paths.home.mkdir(parents=True, exist_ok=True)
    with file_lock(paths.lock) if writing else nullcontext():
        report = doctor(paths, offline=offline, fix=fix, dry_run=dry_run)
    failing = [c for c in report.checks if c.status == "fail"]
    fixable = [c for c in failing if c.fixable]
    next_actions = []
    if fixable and not fix:
        next_actions.append("Run `jobhunt doctor --fix`.")
    if any(c.name.startswith("onboarding.") and c.status != "ok" for c in report.checks):
        next_actions.append("Run the jobhunt-setup skill to finish onboarding.")
    user_actions = [c.fix for c in failing if not c.fixable and c.fix]
    if failing:
        return Result(
            data=report,
            ok=False,
            exit_code=ExitCode.CHECKS_FAILED,
            error=ErrorInfo(
                code="checks_failed",
                message=f"{len(failing)} check(s) failing: " + ", ".join(c.name for c in failing),
            ),
            next_actions=next_actions,
            user_actions=user_actions,
        )
    return Result(data=report, next_actions=next_actions)


# --- schema ---------------------------------------------------------------------------------


class CommandSummary(BaseModel):
    name: str
    summary: str
    writes: bool


class SchemaIndex(BaseModel):
    version: str
    commands: list[CommandSummary]
    exit_codes: dict[str, str] = Field(description="Exit code -> meaning.")


class CommandSchema(BaseModel):
    name: str
    summary: str
    writes: bool
    input: dict[str, Any]
    output: dict[str, Any]


class SchemaAll(BaseModel):
    version: str
    exit_codes: dict[str, str]
    commands: dict[str, CommandSchema]


def _command_schema(name: str) -> CommandSchema:
    spec = REGISTRY[name]
    return CommandSchema(
        name=name,
        summary=spec.summary,
        writes=spec.writes,
        input=input_schema(spec.command),
        output=output_schema(spec),
    )


def _exit_codes() -> dict[str, str]:
    return {str(int(k)): v for k, v in EXIT_CODE_MEANINGS.items()}


@agent_command(cli, "schema", output=SchemaIndex)
@click.argument("command_words", nargs=-1)
@click.option(
    "--all", "all_", is_flag=True, help="Every command's schemas (large: use with --out)."
)
def schema_cmd(command_words: tuple[str, ...], all_: bool) -> Result:
    """Export input and output JSON schemas for commands.

    No argument lists commands; `schema <command>` gives one command's schemas (sub-commands
    as `schema profile set`).
    """
    command = " ".join(command_words) or None
    if command and all_:
        raise ValidationFailed("Pass a command name or --all, not both.")
    if all_:
        data = SchemaAll(
            version=__version__,
            exit_codes=_exit_codes(),
            commands={n: _command_schema(n) for n in sorted(REGISTRY)},
        )
        return Result(data=data)
    if command:
        if command not in REGISTRY:
            raise NotFound(
                f"No command named {command!r}.",
                details={"commands": sorted(REGISTRY)},
                next_actions=["Run `jobhunt schema` to list commands."],
            )
        return Result(data=_command_schema(command))
    index = SchemaIndex(
        version=__version__,
        commands=[
            CommandSummary(name=n, summary=REGISTRY[n].summary, writes=REGISTRY[n].writes)
            for n in sorted(REGISTRY)
        ],
        exit_codes=_exit_codes(),
    )
    return Result(data=index, next_actions=["Run `jobhunt schema <command>` for its full schema."])


# --- discovery ------------------------------------------------------------------------------

register_discovery(cli)
register_fit(cli)
register_ingest(cli)
register_onboarding(cli)
register_answers(cli)
register_apply(cli)
register_prefill(cli)
register_research(cli)
register_essays(cli)


# --- entry point ----------------------------------------------------------------------------


def run(argv: list[str] | None = None) -> int:
    """Run the CLI and return the exit code (tests call this; `main` exits with it)."""
    args = sys.argv[1:] if argv is None else argv
    try:
        rv = cli.main(args=args, prog_name="jobhunt", standalone_mode=False)
    except click.exceptions.Exit as e:  # --help / --version
        return e.exit_code
    except click.exceptions.Abort:
        return int(ExitCode.ERROR)
    except click.ClickException as e:
        err = ValidationFailed(e.format_message(), next_actions=["Run with --help for usage."])
        err.code, err.exit_code = "usage", ExitCode.USAGE
        emit(error_envelope(err), human="--human" in args, out=None, command="usage")
        return int(ExitCode.USAGE)
    return int(rv) if isinstance(rv, int) else 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
