"""The pre-fill browser: a Playwright persistent context on a dedicated profile.

- Headed, so the human sees every tab and clicks Submit themselves.
- Real Chrome when installed (``prefill.browser: auto``), else Playwright's bundled Chromium.
- A dedicated profile at ``~/.jobhunt/browser-profile``; Playwright can't automate the user's
  default Chrome profile, and jobhunt never signs in anywhere on its own.
- The user's normal network: no stealth or anti-detection plugins, no proxies, no spoofed user
  agent. Service workers are blocked because ``page.route`` can't see their requests, which
  would let them slip past the submit guard.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from jobhunt.doctor import find_chrome
from jobhunt.errors import ConfigError
from jobhunt.install import reinstall_command
from jobhunt.models.config import PrefillConfig
from jobhunt.paths import Paths


def channel_for(cfg: PrefillConfig) -> str | None:
    """Playwright's ``channel``: "chrome" for real Chrome, None for bundled Chromium."""
    if cfg.browser == "chrome" or (cfg.browser == "auto" and find_chrome()):
        return "chrome"
    return None


def launch_options(paths: Paths, cfg: PrefillConfig, *, headless: bool = False) -> dict[str, Any]:
    return {
        "user_data_dir": str(paths.browser_profile),
        "channel": channel_for(cfg),
        "headless": headless,
        "service_workers": "block",
        "accept_downloads": False,
        "no_viewport": not headless,  # headed: the window's own size
    }


def require_playwright() -> None:
    if importlib.util.find_spec("playwright") is None:
        raise ConfigError(
            "Pre-fill needs Playwright, which comes with the browser extra.",
            next_actions=[
                f"Reinstall with `{reinstall_command('browser')}`, then run `jobhunt doctor --fix`."
            ],
        )


@contextmanager
def persistent_context(
    paths: Paths, cfg: PrefillConfig, *, headless: bool = False
) -> Iterator[Any]:
    """Open the dedicated-profile browser; closes it on exit."""
    require_playwright()
    from playwright.sync_api import sync_playwright

    paths.ensure()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(**launch_options(paths, cfg, headless=headless))
        try:
            yield ctx
        finally:
            ctx.close()
