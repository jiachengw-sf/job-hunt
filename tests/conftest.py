from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from jobhunt.cli.main import run

REPO = Path(__file__).resolve().parent.parent
PERSONAS_DIR = REPO / "tests" / "personas"
PERSONAS = sorted(p.name for p in PERSONAS_DIR.iterdir() if p.is_dir())


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own HOME and JOBHUNT_HOME: tests never touch real user data."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("JOBHUNT_HOME", str(home / ".jobhunt"))
    monkeypatch.delenv("JOBHUNT_PII_DENYLIST", raising=False)
    return home / ".jobhunt"


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str]):
    """Run jobhunt in-process; returns (exit_code, envelope_dict, stderr)."""

    def _run(*args: str) -> tuple[int, dict, str]:
        code = run(list(args))
        out, err = capsys.readouterr()
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 1, f"expected exactly one stdout line, got {len(lines)}: {out!r}"
        return code, json.loads(lines[0]), err

    return _run


@pytest.fixture
def persona_home(isolated_home: Path):
    """Copy a persona's onboarding files into the isolated data home."""

    def _install(name: str) -> Path:
        isolated_home.mkdir(parents=True, exist_ok=True)
        src = PERSONAS_DIR / name
        for f in ("profile.yaml", "preferences.yaml", "answers.yaml"):
            shutil.copy(src / f, isolated_home / f)
        (isolated_home / "resume").mkdir(exist_ok=True)
        shutil.copy(src / "resume.md", isolated_home / "resume" / "resume.md")
        return isolated_home

    return _install
