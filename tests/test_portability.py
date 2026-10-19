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


def test_pid_alive_probes_without_killing():
    import subprocess
    import sys

    from jobhunt.procs import pid_alive

    assert pid_alive(None) is False
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(child.pid)
        assert pid_alive(child.pid)  # checking twice must not have killed it (Windows os.kill)
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()
    assert pid_alive(child.pid) is False


def test_os_kill_is_never_used_as_a_liveness_probe():
    """os.kill(pid, 0) terminates the process on Windows; use jobhunt.procs.pid_alive."""
    offenders = []
    for f in (REPO / "src").rglob("*.py"):
        if f.name == "procs.py":
            continue
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "kill"
                and len(node.args) == 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == 0
            ):
                offenders.append(f"{f.relative_to(REPO)}:{node.lineno}")
    assert not offenders, offenders
