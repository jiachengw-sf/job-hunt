"""Install the jobhunt skills into each AI tool's skill directory.

Paths and settings (checked against the Claude Code and Codex docs, 2026-10):

- Claude Code: ``~/.claude/skills/<name>/SKILL.md`` (user) or ``.claude/skills/`` (project).
  ``disable-model-invocation: true`` in the frontmatter makes a skill explicit-only (``/name``).
- Codex: ``~/.agents/skills/<name>/SKILL.md`` (user) or ``.agents/skills/`` (repo).
  ``agents/openai.yaml`` with ``policy.allow_implicit_invocation: false`` makes it explicit-only
  (``$name``).

The repo's skills use open-spec frontmatter only. The tool-specific setting for the explicit-only
apply skill is added to that tool's copy alone, so the explicit-only skills are always copied,
never symlinked. Each installed skill carries a ``.jobhunt-skill`` marker; a directory without
it is the user's own and is never overwritten or removed.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Literal

from jobhunt import __version__

Tool = Literal["claude-code", "codex"]
Mode = Literal["copy", "symlink"]
EXPLICIT_ONLY = {"jobhunt-apply"}
MARKER = ".jobhunt-skill"
CODEX_SIDECAR = "policy:\n  allow_implicit_invocation: false\n"


def source_dir() -> Path:
    """The skills shipped in the package, or the repo's ``skills/`` in a source checkout."""
    packaged = Path(str(resources.files("jobhunt"))) / "_skills"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "skills"


def skill_names() -> list[str]:
    return sorted(p.name for p in source_dir().iterdir() if (p / "SKILL.md").is_file())


def target_dir(tool: Tool, scope: Literal["user", "project"], project: Path) -> Path:
    base = Path.home() if scope == "user" else project
    return base / (".claude" if tool == "claude-code" else ".agents") / "skills"


def _add_claude_explicit(skill_md: Path) -> None:
    text = skill_md.read_text(encoding="utf-8")
    head, sep, rest = text.partition("\n---\n")
    if "disable-model-invocation:" not in head:
        head += "\ndisable-model-invocation: true"
    skill_md.write_text(head + sep + rest, encoding="utf-8")


@dataclass
class Outcome:
    tool: str
    directory: str
    installed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    explicit_only: list[str] = field(default_factory=list)


def _owned(dest: Path) -> bool:
    return dest.is_symlink() or (dest / MARKER).is_file()


def _same(a: Path, b: Path) -> bool:
    fa = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file() and p.name != MARKER)
    fb = sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file() and p.name != MARKER)
    return fa == fb and all((a / r).read_bytes() == (b / r).read_bytes() for r in fa)


def _build(src: Path, staging: Path, tool: Tool, name: str) -> None:
    shutil.copytree(src, staging)
    if name in EXPLICIT_ONLY:
        if tool == "claude-code":
            _add_claude_explicit(staging / "SKILL.md")
        else:
            (staging / "agents").mkdir(exist_ok=True)
            (staging / "agents" / "openai.yaml").write_text(CODEX_SIDECAR, encoding="utf-8")
    (staging / MARKER).write_text(
        json.dumps({"version": __version__, "tool": tool}) + "\n", encoding="utf-8"
    )


def install(tool: Tool, dest_root: Path, *, mode: Mode, force: bool, dry_run: bool) -> Outcome:
    out = Outcome(tool=tool, directory=str(dest_root))
    src_root = source_dir()
    for name in skill_names():
        src, dest = src_root / name, dest_root / name
        explicit = name in EXPLICIT_ONLY
        if explicit:
            out.explicit_only.append(name)
        if dest.exists() and not _owned(dest) and not force:
            out.skipped[name] = "a skill with this name that jobhunt didn't install is there"
            continue
        link = mode == "symlink" and not explicit
        if link and dest.is_symlink() and dest.resolve() == src.resolve():
            out.unchanged.append(name)
            continue
        staging = dest_root / f".{name}.jobhunt-new"
        if not link:
            if dry_run:
                out.installed.append(name)
                continue
            dest_root.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(staging, ignore_errors=True)
            _build(src, staging, tool, name)
            if dest.is_dir() and not dest.is_symlink() and _same(staging, dest):
                shutil.rmtree(staging)
                out.unchanged.append(name)
                continue
        elif dry_run:
            out.installed.append(name)
            continue
        dest_root.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        elif dest.exists():
            shutil.rmtree(dest)
        if link:
            dest.symlink_to(src.resolve(), target_is_directory=True)
        else:
            staging.replace(dest)
        out.installed.append(name)
    return out


def remove(dest_root: Path, *, dry_run: bool) -> list[str]:
    gone = []
    for name in skill_names():
        dest = dest_root / name
        if not dest.exists() and not dest.is_symlink():
            continue
        if not _owned(dest):
            continue
        gone.append(name)
        if dry_run:
            continue
        if dest.is_symlink():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    return gone


def explicit_ok(tool: str, skill_dir: Path) -> bool:
    """Is the explicit-only setting in place for this installed skill?"""
    if tool == "claude-code":
        md = skill_dir / "SKILL.md"
        return md.is_file() and "disable-model-invocation: true" in md.read_text(encoding="utf-8")
    side = skill_dir / "agents" / "openai.yaml"
    return side.is_file() and "allow_implicit_invocation: false" in side.read_text(encoding="utf-8")
