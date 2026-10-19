"""Portability guards: things that pass on macOS/Linux and break on Windows."""

from __future__ import annotations

import ast

from tests.conftest import REPO


def test_every_text_read_and_write_names_its_encoding():
    """Windows defaults to cp1252; postings, fixtures and user files are UTF-8."""
    missing = []
    for f in [*(REPO / "src").rglob("*.py"), *(REPO / "tests").rglob("*.py")]:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            name = node.func.attr
            if name not in ("read_text", "write_text"):
                continue
            positional = 1 if name == "read_text" else 2
            if len(node.args) >= positional or any(k.arg == "encoding" for k in node.keywords):
                continue
            missing.append(f"{f.relative_to(REPO)}:{node.lineno} {name}")
    assert not missing, "add encoding='utf-8':\n" + "\n".join(missing)
