from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from jobhunt.cli.main import run
from jobhunt.doctor import find_chrome, playwright_browsers_dir

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
        if (src / "watchlist.yaml").exists():
            shutil.copy(src / "watchlist.yaml", isolated_home / "watchlist.yaml")
        (isolated_home / "resume").mkdir(exist_ok=True)
        shutil.copy(src / "resume.md", isolated_home / "resume" / "resume.md")
        return isolated_home

    return _install


# Playwright finds its bundled Chromium under the real home; tests redirect HOME, so pin it here.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(playwright_browsers_dir()))


@pytest.fixture(scope="session")
def chromium():
    """A headless browser for the pre-fill tests (real Chrome if installed). Skips without one."""
    sync_api = pytest.importorskip("playwright.sync_api")
    p = sync_api.sync_playwright().start()
    try:
        try:
            b = p.chromium.launch(channel="chrome" if find_chrome() else None, headless=True)
        except Exception as e:  # no browser binary installed
            pytest.skip(f"no browser for Playwright: {e}")
        yield b
        b.close()
    finally:
        p.stop()


@pytest.fixture
def browser_context(chromium):
    ctx = chromium.new_context(service_workers="block")
    yield ctx
    ctx.close()


@pytest.fixture(scope="session")
def fx_pages(tmp_path_factory) -> Path:
    """Application-form fixtures plus the browser pages, laid over each other."""
    forms = REPO / "tests" / "fixtures" / "forms"
    d = tmp_path_factory.mktemp("pages")
    shutil.copytree(forms / "overlay", d, dirs_exist_ok=True)
    shutil.copytree(forms / "pages", d, dirs_exist_ok=True)
    return d
