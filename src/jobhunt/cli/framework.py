"""How agent-facing commands are declared.

``@agent_command`` turns a plain function into a Click command that:
  - adds ``--human`` and ``--out`` (and ``--dry-run`` when ``writes=True``)
  - never prompts
  - prints exactly one JSON envelope on stdout (logs go to stderr)
  - maps errors to stable exit codes
  - registers its input/output schema for ``jobhunt schema``
"""

from __future__ import annotations

import functools
import logging
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click
from pydantic import BaseModel

from jobhunt.envelope import (
    Envelope,
    ErrorInfo,
    Hints,
    Page,
    render_human_default,
    spill_if_large,
    to_json,
)
from jobhunt.errors import ExitCode, JobhuntError
from jobhunt.paths import get_paths

log = logging.getLogger("jobhunt")


@dataclass
class Result:
    """What a command function returns."""

    data: Any = None
    next_actions: list[str] = field(default_factory=list)
    user_actions: list[str] = field(default_factory=list)
    page: Page | None = None
    ok: bool = True
    error: ErrorInfo | None = None
    exit_code: int = ExitCode.OK


@dataclass
class CommandSpec:
    name: str
    summary: str
    output_model: type[BaseModel] | None
    writes: bool
    command: click.Command


REGISTRY: dict[str, CommandSpec] = {}

HumanRenderer = Callable[[Envelope], str]


def emit(
    envelope: Envelope,
    *,
    human: bool,
    out: Path | None,
    command: str,
    renderer: HumanRenderer | None = None,
) -> None:
    envelope = spill_if_large(envelope, out=out, spill_dir=_spill_dir(), command=command)
    if human:
        click.echo((renderer or render_human_default)(envelope))
    else:
        click.echo(to_json(envelope))


def _spill_dir() -> Path:
    paths = get_paths()
    if paths.home.is_dir():
        return paths.out
    import tempfile

    return Path(tempfile.gettempdir()) / "jobhunt-out"


def error_envelope(err: JobhuntError) -> Envelope:
    return Envelope(
        ok=False,
        error=ErrorInfo(code=err.code, message=err.message, details=err.details),
        hints=Hints(next_actions=err.next_actions, user_actions=err.user_actions),
    )


def _serialize(data: Any) -> Any:
    if isinstance(data, BaseModel):
        return data.model_dump(mode="json")
    return data


def agent_command(
    group: click.Group,
    name: str,
    *,
    output: type[BaseModel] | None = None,
    writes: bool = False,
    render: HumanRenderer | None = None,
) -> Callable[[Callable[..., Result]], click.Command]:
    def decorate(fn: Callable[..., Result]) -> click.Command:
        doc = (fn.__doc__ or "").strip()
        summary = doc.splitlines()[0] if doc else name

        @functools.wraps(fn)
        def callback(human: bool, out: Path | None, **kwargs: Any) -> int:
            try:
                result = fn(**kwargs)
                env = Envelope(
                    ok=result.ok,
                    data=_serialize(result.data),
                    error=result.error,
                    hints=Hints(next_actions=result.next_actions, user_actions=result.user_actions),
                    page=result.page,
                )
                code = int(result.exit_code)
            except JobhuntError as e:
                env, code = error_envelope(e), int(e.exit_code)
            except Exception as e:  # a bug: still answer with an envelope
                log.debug("internal error", exc_info=True)
                env = Envelope(
                    ok=False,
                    error=ErrorInfo(
                        code="internal",
                        message=f"Internal error: {type(e).__name__}: {e}",
                        details={"traceback": traceback.format_exc(limit=5)},
                    ),
                    hints=Hints(next_actions=["Re-run with -v and report the bug."]),
                )
                code = int(ExitCode.INTERNAL)
            emit(env, human=human, out=out, command=name, renderer=render)
            return code

        params: list[click.Parameter] = list(reversed(getattr(fn, "__click_params__", [])))
        params += [
            click.Option(["--human"], is_flag=True, help="Readable output instead of JSON."),
            click.Option(
                ["--out"],
                type=click.Path(dir_okay=False, path_type=Path),
                default=None,
                help="Write the full data to this file; stdout gets a pointer.",
            ),
        ]
        if writes:
            params.append(
                click.Option(
                    ["--dry-run"], is_flag=True, help="Show what would change; write nothing."
                )
            )
        cmd = click.Command(
            name,
            callback=callback,
            params=params,
            help=doc,
            short_help=summary,
            context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
        )
        group.add_command(cmd)
        REGISTRY[name] = CommandSpec(name, summary, output, writes, cmd)
        return cmd

    return decorate


# --- input schema from click params ---------------------------------------------------------


def _param_schema(p: click.Parameter) -> dict[str, Any]:
    t = p.type
    s: dict[str, Any]
    if isinstance(p, click.Option) and p.is_flag:
        s = {"type": "boolean"}
    elif isinstance(t, click.Choice):
        s = {"type": "string", "enum": list(t.choices)}
    elif isinstance(t, click.types.IntParamType):
        s = {"type": "integer"}
    elif isinstance(t, click.types.FloatParamType):
        s = {"type": "number"}
    elif isinstance(t, click.Path):
        s = {"type": "string", "format": "path"}
    else:
        s = {"type": "string"}
    if p.multiple or (p.nargs == -1):
        s = {"type": "array", "items": s}
    help_text = getattr(p, "help", None)
    if help_text:
        s["description"] = help_text
    # click >= 8.2 uses a sentinel object for "no default"; only export plain JSON values
    if isinstance(p.default, str | int | float | bool) or (
        isinstance(p.default, list | tuple) and p.default
    ):
        s["default"] = list(p.default) if isinstance(p.default, tuple) else p.default
    return s


def input_schema(cmd: click.Command) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required: list[str] = []
    for p in cmd.params:
        if isinstance(p, click.Argument):
            key = p.name or ""
            props[key] = {**_param_schema(p), "x-cli": "argument"}
        else:
            assert isinstance(p, click.Option)
            key = max(p.opts, key=len)
            props[key] = {**_param_schema(p), "x-cli": "option"}
        if p.required:
            required.append(key)
    return {"type": "object", "properties": props, "required": required}


def output_schema(spec: CommandSpec) -> dict[str, Any]:
    env = Envelope.model_json_schema()
    if spec.output_model is not None:
        data_schema = spec.output_model.model_json_schema()
        defs = {**env.pop("$defs", {}), **data_schema.pop("$defs", {})}
        env["properties"]["data"] = {"anyOf": [data_schema, {"type": "null"}]}
        env["$defs"] = defs
    return env


def configure_logging(verbose: int) -> None:
    level = logging.WARNING - 10 * min(verbose, 2)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("jobhunt")
    root.handlers[:] = [handler]
    root.setLevel(level)
