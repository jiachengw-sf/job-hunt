"""The agent-facing CLI contract: one JSON envelope, stable exit codes, schemas, no prompts."""

from __future__ import annotations

import json

import pytest

from jobhunt.cli.framework import REGISTRY
from jobhunt.cli.main import cli as cli_group
from jobhunt.cli.main import run
from jobhunt.envelope import MAX_CHARS, Envelope
from jobhunt.errors import ExitCode

ENVELOPE_KEYS = {"ok", "data", "error", "hints", "page"}


def test_envelope_shape(cli):
    code, env, _ = cli("schema")
    assert code == 0
    assert set(env) == ENVELOPE_KEYS
    assert env["ok"] is True and env["error"] is None
    assert set(env["hints"]) == {"next_actions", "user_actions"}
    Envelope.model_validate(env)


def _leaf_names(group, prefix=""):
    import click

    out = set()
    for name, cmd in group.commands.items():
        full = f"{prefix}{name}"
        if isinstance(cmd, click.Group):
            out |= _leaf_names(cmd, full + " ")
        else:
            out.add(full)
    return out


def test_every_command_registered_and_has_short_help():
    assert _leaf_names(cli_group) == set(REGISTRY)
    for name, spec in REGISTRY.items():
        assert spec.summary and spec.summary != name, f"{name} needs a docstring"
        assert len(spec.summary) <= 90, f"{name} summary is too long for short --help"


def test_writing_commands_have_dry_run():
    for name, spec in REGISTRY.items():
        opts = {o for p in spec.command.params for o in getattr(p, "opts", [])}
        assert {"--human", "--out"} <= opts, name
        if spec.writes:
            assert "--dry-run" in opts, f"{name} writes but has no --dry-run"


def test_no_submit_command_exists():
    """Non-negotiable #1: no code path submits an application."""
    banned = {"submit", "apply-submit", "send", "auto-apply"}
    names = _leaf_names(cli_group)
    assert not banned & {w for n in names for w in n.split()}
    for name in names:
        assert "submit" not in name


def test_unknown_command_is_usage_error_envelope(cli):
    code, env, _ = cli("definitely-not-a-command")
    assert code == ExitCode.USAGE
    assert env["ok"] is False and env["error"]["code"] == "usage"


def test_unknown_option_is_usage_error_envelope(cli):
    code, env, _ = cli("doctor", "--no-such-flag")
    assert code == ExitCode.USAGE
    assert env["error"]["code"] == "usage"


def test_help_works_for_every_command(capsys):
    assert run(["--help"]) == 0
    for name in REGISTRY:
        assert run([*name.split(), "--help"]) == 0
    out = capsys.readouterr().out
    assert "doctor" in out


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_schema_for_each_command(cli, name):
    code, env, _ = cli("schema", *name.split())
    assert code == 0
    data = env["data"]
    assert data["name"] == name
    assert data["input"]["type"] == "object"
    assert "--human" in data["input"]["properties"]
    out_props = data["output"]["properties"]
    assert set(out_props) == ENVELOPE_KEYS
    json.dumps(data)  # serializable


def test_schema_unknown_command_is_not_found(cli):
    code, env, _ = cli("schema", "nope")
    assert code == ExitCode.NOT_FOUND
    assert env["error"]["code"] == "not_found"


def test_schema_all_spills_to_out_file(cli, tmp_path):
    out = tmp_path / "schema.json"
    code, env, _ = cli("schema", "--all", "--out", str(out))
    assert code == 0
    assert env["page"]["out_file"] == str(out)
    full = json.loads(out.read_text(encoding="utf-8"))
    assert set(full["commands"]) == set(REGISTRY)
    assert full["exit_codes"]["9"]


def test_output_cap_spills_to_file(capsys, monkeypatch):
    """Without --out, anything over MAX_CHARS is written to a file and stdout stays small."""
    monkeypatch.setattr("jobhunt.envelope.MAX_CHARS", 500)
    assert run(["schema", "--all"]) == 0
    out = capsys.readouterr().out
    assert len(out) <= MAX_CHARS
    env = json.loads(out)
    assert env["page"]["out_file"]
    with open(env["page"]["out_file"], encoding="utf-8") as f:
        assert "commands" in json.load(f)


def test_human_flag_prints_text_not_json(capsys):
    assert run(["schema", "--human"]) == 0
    out = capsys.readouterr().out
    assert out.lstrip()[0] != "{"
    assert "doctor" in out


def test_exit_codes_are_distinct():
    values = [int(c) for c in ExitCode]
    assert len(values) == len(set(values))
