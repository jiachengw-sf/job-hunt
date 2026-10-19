"""Reinstall and upgrade hints match how jobhunt was installed: PyPI, git, a checkout, a wheel."""

from __future__ import annotations

import pytest

from jobhunt import install

CHECKOUT = {"url": "file:///home/dev/jobhunt", "dir_info": {}}
WHEEL = {"url": "file:///tmp/dist/jobhunt-0.1.0-py3-none-any.whl", "archive_info": {}}
GIT = {
    "url": "https://github.com/someone/jobhunt.git",
    "vcs_info": {"vcs": "git", "commit_id": "abc123", "requested_revision": "main"},
}
EDITABLE = {"url": "file:///home/dev/jobhunt", "dir_info": {"editable": True}}


def setup(monkeypatch, method: str, direct) -> None:
    monkeypatch.setattr(install, "detect_install_method", lambda: method)
    monkeypatch.setattr(install, "_direct_url", lambda: direct)


@pytest.mark.parametrize(
    ("method", "direct", "expected"),
    [
        ("uv-tool", None, "uv tool install --reinstall 'jobhunt[browser]'"),
        ("pipx", None, "pipx install --force 'jobhunt[browser]'"),
        (
            "uv-tool",
            CHECKOUT,
            "uv tool install --reinstall 'jobhunt[browser] @ file:///home/dev/jobhunt'",
        ),
        (
            "uv-tool",
            WHEEL,
            "uv tool install --reinstall "
            "'jobhunt[browser] @ file:///tmp/dist/jobhunt-0.1.0-py3-none-any.whl'",
        ),
        (
            "uv-tool",
            GIT,
            "uv tool install --reinstall "
            "'jobhunt[browser] @ git+https://github.com/someone/jobhunt.git@main'",
        ),
        ("editable", EDITABLE, "uv sync --extra browser"),
    ],
)
def test_reinstall_command_matches_the_install(monkeypatch, method, direct, expected):
    setup(monkeypatch, method, direct)
    assert install.reinstall_command("browser") == expected


@pytest.mark.parametrize(
    ("method", "direct", "expected"),
    [
        ("uv-tool", None, "`uv tool upgrade jobhunt`"),
        ("pipx", None, "`pipx upgrade jobhunt`"),
        ("editable", EDITABLE, "`git pull && uv sync` in your checkout"),
        (
            "uv-tool",
            GIT,
            "`uv tool install --reinstall 'jobhunt @ git+https://github.com/someone/jobhunt.git@main'`",
        ),
        (
            "uv-tool",
            CHECKOUT,
            "update /home/dev/jobhunt, then run "
            "`uv tool install --reinstall 'jobhunt @ file:///home/dev/jobhunt'`",
        ),
    ],
)
def test_upgrade_hint_matches_the_install(monkeypatch, method, direct, expected):
    setup(monkeypatch, method, direct)
    assert install.upgrade_hint() == expected


def test_doctor_browser_fix_uses_the_install_source(monkeypatch):
    import importlib.util

    from jobhunt import doctor

    setup(monkeypatch, "uv-tool", CHECKOUT)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    check = doctor.check_browser()
    assert check.fix == (
        "Reinstall with the browser extra: "
        "`uv tool install --reinstall 'jobhunt[browser] @ file:///home/dev/jobhunt'`."
    )


def test_this_checkout_is_detected_as_a_development_install():
    assert install.detect_install_method() in ("editable", "source")
