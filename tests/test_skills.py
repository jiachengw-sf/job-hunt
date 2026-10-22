"""The eight portable skills: open-spec frontmatter, size limits, and commands that exist."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import jobhunt.cli.main  # noqa: F401  (registers every command)
from jobhunt.cli.framework import REGISTRY, default_groups
from jobhunt.doctor import SKILL_NAMES
from tests.conftest import REPO

SKILLS = REPO / "skills"
OPEN_SPEC = {"name", "description", "license", "compatibility", "metadata"}
NAMES = sorted(p.name for p in SKILLS.iterdir() if p.is_dir())

DEFAULT_GROUPS = default_groups()  # e.g. bare `jobhunt apps`


def _split(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), path
    _, fm, body = text.split("---\n", 2)
    return yaml.safe_load(fm), body


def test_the_eight_skills():
    assert sorted(SKILL_NAMES) == NAMES
    assert len(NAMES) == 8


@pytest.mark.parametrize("name", NAMES)
def test_frontmatter_is_portable(name):
    fm, body = _split(SKILLS / name / "SKILL.md")
    assert set(fm) <= OPEN_SPEC, set(fm) - OPEN_SPEC
    assert fm["name"] == name
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) and len(name) <= 64
    d = fm["description"]
    assert 50 < len(d) <= 500, len(d)
    assert "<" not in d and ">" not in d
    assert len(body.splitlines()) < 500


@pytest.mark.parametrize("name", NAMES)
def test_references_exist_one_level_deep(name):
    root = SKILLS / name
    _, body = _split(root / "SKILL.md")
    links = re.findall(r"\]\(([^)#]+)\)", body)
    assert links, "detail belongs in references/"
    for link in links:
        assert re.fullmatch(r"references/[a-z0-9-]+\.md", link), link
        assert (root / link).is_file(), link
    for f in root.rglob("*"):
        rel = f.relative_to(root)
        assert len(rel.parts) <= 2, f"too deep: {rel}"


@pytest.mark.parametrize("name", NAMES)
def test_only_real_commands_and_no_tool_specific_paths(name):
    for f in (SKILLS / name).rglob("*.md"):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"\$\{?(CLAUDE|CODEX)_", text), f
        assert "~/.claude" not in text and "~/.codex" not in text, f
        for cmd in re.findall(r"`jobhunt ([a-z][a-z -]*)", text):
            words = cmd.split()
            ok = " ".join(words[:2]) in REGISTRY or words[0] in REGISTRY.keys() | DEFAULT_GROUPS
            assert ok, f"{f.name}: `jobhunt {cmd}` is not a command"


def test_safety_rules_are_stated():
    apply_fm, apply_body = _split(SKILLS / "jobhunt-apply" / "SKILL.md")
    assert "explicitly" in apply_fm["description"] and "Never submits" in apply_fm["description"]
    assert "never click Submit" in apply_body
    _, research = _split(SKILLS / "jobhunt-research" / "SKILL.md")
    assert "5 web searches and 8 page fetches" in research and "No subagents" in research
    _, daily = _split(SKILLS / "jobhunt-daily" / "SKILL.md")
    assert "Never run `jobhunt prefill`" in daily
    _, outreach = _split(SKILLS / "jobhunt-outreach" / "SKILL.md")
    assert "never sends anything" in outreach
    for name in NAMES:
        _, body = _split(SKILLS / name / "SKILL.md")
        if name != "jobhunt-setup":  # setup reads only the user's own resume
            assert "untrusted" in body.lower(), name
