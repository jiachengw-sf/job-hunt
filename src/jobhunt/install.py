"""How jobhunt was installed, and the exact command to reinstall it with an extra or upgrade it.

jobhunt may come from PyPI, a git URL, a local checkout or a wheel file. A hint like
`uv tool install 'jobhunt[browser]'` only works for the first, so every "reinstall with the X
extra" message asks this module for a command that matches the user's own install.
"""

from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any


def _direct_url() -> dict[str, Any] | None:
    """PEP 610 metadata: present when installed from a URL, path or VCS, absent from an index."""
    try:
        raw = importlib.metadata.distribution("jobhunt").read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return None
    return json.loads(raw) if raw else None


def detect_install_method() -> str:
    """uv-tool | pipx | editable | source (running from a tree, not installed) | other."""
    prefix = Path(sys.prefix).as_posix()
    if "/uv/tools/" in prefix:
        return "uv-tool"
    if "/pipx/venvs/" in prefix:
        return "pipx"
    try:
        importlib.metadata.distribution("jobhunt")
    except importlib.metadata.PackageNotFoundError:
        return "source"
    if (_direct_url() or {}).get("dir_info", {}).get("editable"):
        return "editable"
    return "other"


def _requirement(extra: str | None, direct: dict[str, Any] | None) -> str:
    name = f"jobhunt[{extra}]" if extra else "jobhunt"
    if not direct:
        return name  # from an index (PyPI)
    url = direct["url"]
    vcs = direct.get("vcs_info")
    if vcs:
        rev = vcs.get("requested_revision")
        url = f"{vcs.get('vcs', 'git')}+{url}" + (f"@{rev}" if rev else "")
    return f"{name} @ {url}"


def reinstall_command(extra: str | None = None) -> str:
    """The command that reinstalls jobhunt from the same source, adding `extra` if given."""
    method = detect_install_method()
    direct = _direct_url()
    if method in ("editable", "source"):
        return f"uv sync --extra {extra}" if extra else "uv sync"  # run in the checkout
    req = _requirement(extra, direct)
    if method == "pipx":
        return f"pipx install --force '{req}'"
    return f"uv tool install --reinstall '{req}'"


def upgrade_hint() -> str:
    """How to upgrade jobhunt from the same source, ready for a message (commands in backticks)."""
    method = detect_install_method()
    direct = _direct_url()
    if method in ("editable", "source"):
        return "`git pull && uv sync` in your checkout"
    if not direct:  # from an index
        return "`pipx upgrade jobhunt`" if method == "pipx" else "`uv tool upgrade jobhunt`"
    if direct.get("vcs_info"):  # a git URL: reinstalling fetches the latest
        return f"`{reinstall_command()}`"
    # A local checkout or wheel: update it first, then reinstall from it.
    where = direct["url"].removeprefix("file://")
    return f"update {where}, then run `{reinstall_command()}`"
