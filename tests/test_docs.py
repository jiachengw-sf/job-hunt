"""User-facing docs: every `jobhunt ...` command they name exists, and every local link resolves."""

from __future__ import annotations

import re

import click
import pytest
import yaml

import jobhunt.cli.main  # noqa: F401  (registers every command)
from jobhunt.cli.framework import REGISTRY
from tests.conftest import PERSONAS, PERSONAS_DIR, REPO

DOCS = ["README.md", "CONTRIBUTING.md", "AGENTS.md"]


def _docs():
    return [d for d in DOCS if (REPO / d).exists()]


@pytest.mark.parametrize("doc", _docs())
def test_named_commands_exist(doc):
    text = (REPO / doc).read_text(encoding="utf-8")
    cmds = re.findall(r"`jobhunt ([a-z][a-z-]*(?: [a-z][a-z-]*)?)", text)
    for block in re.findall(r"```bash\n(.*?)```", text, re.S):
        cmds += re.findall(r"^jobhunt ([a-z][a-z-]*(?: [a-z][a-z-]*)?)", block, re.M)
    assert cmds
    for cmd in cmds:
        words = cmd.split()
        ok = " ".join(words) in REGISTRY or words[0] in REGISTRY
        assert ok, f"{doc}: `jobhunt {cmd}` is not a command"


@pytest.mark.parametrize("doc", _docs())
def test_named_flags_exist(doc):
    """`jobhunt <cmd> ... --flag` in a doc: the command accepts that flag."""
    from jobhunt.cli.main import cli

    text = (REPO / doc).read_text(encoding="utf-8")
    for line in re.findall(r"jobhunt [a-z][^`\n]*", text):
        words = line.split()[1:]
        cmd: click.Command = cli
        while isinstance(cmd, click.Group) and words and words[0] in cmd.commands:
            cmd = cmd.commands[words.pop(0)]
        opts = {o for p in cmd.params for o in getattr(p, "opts", [])} | {"--help"}
        for w in words:
            if w.startswith("--"):
                assert w.split("=")[0] in opts, f"{doc}: {line!r} has no {w}"


@pytest.mark.parametrize("doc", _docs())
def test_local_links_resolve(doc):
    text = (REPO / doc).read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)#\s]+)\)", text):
        if "://" in target or target.startswith("mailto:"):
            continue
        assert (REPO / target).exists(), f"{doc}: broken link {target}"


def test_demo_uses_a_synthetic_persona_only():
    """The demo tape runs in a scratch home with a synthetic persona and offline fixtures."""
    tape = (REPO / "docs" / "demo" / "demo.tape").read_text(encoding="utf-8")
    setup = (REPO / "docs" / "demo" / "setup.sh").read_text(encoding="utf-8")
    persona = re.search(r"^PERSONA=(\S+)$", setup, re.M)
    assert persona and persona.group(1) in PERSONAS
    assert "Output docs/demo.gif" in tape and "source docs/demo/setup.sh" in tape
    for var in ("HOME", "JOBHUNT_HOME", "JOBHUNT_FIXTURES"):
        assert f"{var}=" in setup  # never the real home or the network
    assert (REPO / "docs" / "demo.gif").stat().st_size < 5_000_000
    for persona_dir in PERSONAS:  # no contact detail or legal name, even a synthetic one
        ident = yaml.safe_load(
            (PERSONAS_DIR / persona_dir / "answers.yaml").read_text(encoding="utf-8")
        ).get("identity", {})
        for k in ("email", "phone", "legal_last_name", "address_line1"):
            if ident.get(k):
                assert ident[k] not in tape, k
