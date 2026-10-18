"""The pre-fill browser: dedicated persistent profile, real Chrome when present, no stealth."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from jobhunt.models.config import PrefillConfig
from jobhunt.paths import get_paths
from jobhunt.prefill import browser


def test_channel_prefers_real_chrome(monkeypatch):
    monkeypatch.setattr(browser, "find_chrome", lambda: "/opt/chrome")
    assert browser.channel_for(PrefillConfig()) == "chrome"
    assert browser.channel_for(PrefillConfig(browser="chromium")) is None
    monkeypatch.setattr(browser, "find_chrome", lambda: None)
    assert browser.channel_for(PrefillConfig()) is None
    assert browser.channel_for(PrefillConfig(browser="chrome")) == "chrome"


def test_launch_options_are_plain(isolated_home):
    opts = browser.launch_options(get_paths(), PrefillConfig())
    assert opts["user_data_dir"] == str(isolated_home / "browser-profile")
    assert opts["headless"] is False
    assert opts["service_workers"] == "block"  # page.route can't see service-worker requests
    for banned in ("proxy", "args", "ignore_default_args", "user_agent", "extra_http_headers"):
        assert banned not in opts


def test_persistent_profile_launches(isolated_home):
    """A real launch, in a subprocess (one Playwright sync instance per process)."""
    pytest.importorskip("playwright")
    code = (
        "import json\n"
        "from jobhunt.models.config import PrefillConfig\n"
        "from jobhunt.paths import get_paths\n"
        "from jobhunt.prefill.browser import persistent_context\n"
        "with persistent_context(get_paths(), PrefillConfig(), headless=True) as ctx:\n"
        "    page = ctx.new_page()\n"
        "    page.set_content('<p id=x>ok</p>')\n"
        "    print(json.dumps({'text': page.inner_text('#x')}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env=os.environ
    )
    if proc.returncode != 0 and "Executable doesn't exist" in proc.stderr:
        pytest.skip("no browser binary installed")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["text"] == "ok"
    assert any((isolated_home / "browser-profile").iterdir())
