"""`jobhunt doctor`: environment checks. Every non-ok check names the exact fix."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from jobhunt.config import USER_FILES, load_config, load_user_file, tracker_xlsx_path
from jobhunt.errors import ConfigError, OnboardingIncomplete
from jobhunt.http import probe
from jobhunt.paths import Paths, package_file, secure_file
from jobhunt.store import SCHEMA_VERSION, connect, current_version, migrate

Status = Literal["ok", "warn", "fail", "skip"]

SKILL_NAMES = [
    "jobhunt-setup",
    "jobhunt-daily",
    "jobhunt-judge",
    "jobhunt-research",
    "jobhunt-apply",
    "jobhunt-essays",
    "jobhunt-outreach",
]

# Public, documented endpoints the sources use. Any HTTP response counts as reachable.
SOURCE_PROBES = {
    "simplify (GitHub raw)": "https://raw.githubusercontent.com/",
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/",
    "lever": "https://api.lever.co/v0/postings/",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/",
}


class Check(BaseModel):
    name: str
    status: Status
    summary: str
    fix: str | None = Field(None, description="Exact step that resolves a warn/fail.")
    fixable: bool = Field(False, description="`jobhunt doctor --fix` can resolve it.")


class DoctorReport(BaseModel):
    checks: list[Check]
    counts: dict[str, int]
    fixed: list[str] = Field(
        default_factory=list, description="Fixes applied (or planned, with --dry-run)."
    )
    dry_run: bool = False


# --- individual checks ----------------------------------------------------------------------


def check_python() -> Check:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 11):
        return Check(name="python", status="ok", summary=f"Python {ver}")
    return Check(
        name="python",
        status="fail",
        summary=f"Python {ver} is too old (need 3.11+)",
        fix="Install Python 3.11+ (e.g. `uv python install 3.12`) and reinstall jobhunt.",
    )


def detect_install_method() -> str:
    prefix = Path(sys.prefix).as_posix()
    if "/uv/tools/" in prefix:
        return "uv-tool"
    if "/pipx/venvs/" in prefix:
        return "pipx"
    try:
        dist = importlib.metadata.distribution("jobhunt")
        raw = dist.read_text("direct_url.json")
        if raw and json.loads(raw).get("dir_info", {}).get("editable"):
            return "editable"
    except importlib.metadata.PackageNotFoundError:
        return "source"
    return "other"


def check_install() -> Check:
    method = detect_install_method()
    if method in ("uv-tool", "pipx"):
        return Check(name="install", status="ok", summary=f"Installed with {method}")
    if method in ("editable", "source"):
        return Check(name="install", status="ok", summary=f"Development install ({method})")
    return Check(
        name="install",
        status="warn",
        summary="Installed into a shared environment",
        fix="Reinstall in isolation: `uv tool install jobhunt`"
        " (or `uv tool install .` from a checkout).",
    )


def check_data_dir(paths: Paths) -> Check:
    missing = paths.missing_directories()
    if not missing:
        return Check(name="data_dir", status="ok", summary=f"Data directory {paths.home}")
    return Check(
        name="data_dir",
        status="fail",
        summary=f"Missing {len(missing)} directories under {paths.home}",
        fix="Run `jobhunt doctor --fix`.",
        fixable=True,
    )


def check_config(paths: Paths) -> Check:
    try:
        load_config(paths)
    except ConfigError as e:
        return Check(
            name="config",
            status="fail",
            summary=e.message,
            fix=e.next_actions[0] if e.next_actions else f"Fix {paths.config}.",
        )
    if not paths.config.exists():
        return Check(
            name="config",
            status="warn",
            summary=f"No {paths.config}; using defaults",
            fix="Run `jobhunt doctor --fix` to write a starter config.",
            fixable=True,
        )
    return Check(name="config", status="ok", summary=f"{paths.config} is valid")


def check_store(paths: Paths) -> Check:
    if not paths.db.exists():
        return Check(
            name="store",
            status="fail",
            summary="Database not created yet",
            fix="Run `jobhunt doctor --fix`.",
            fixable=True,
        )
    conn = connect(paths.db)
    try:
        v = current_version(conn)
    finally:
        conn.close()
    if v == SCHEMA_VERSION:
        return Check(name="store", status="ok", summary=f"Database schema v{v}")
    if v > SCHEMA_VERSION:
        return Check(
            name="store",
            status="fail",
            summary=f"Database schema v{v} is newer than this jobhunt (v{SCHEMA_VERSION})",
            fix="Upgrade jobhunt: `uv tool upgrade jobhunt`.",
        )
    return Check(
        name="store",
        status="fail",
        summary=f"Database schema v{v} needs migrating to v{SCHEMA_VERSION}",
        fix="Run `jobhunt doctor --fix`.",
        fixable=True,
    )


def check_onboarding(paths: Paths) -> list[Check]:
    out = []
    for kind in USER_FILES:
        name = f"onboarding.{kind}"
        try:
            load_user_file(kind, paths)
            out.append(Check(name=name, status="ok", summary=f"{kind}.yaml is valid"))
        except OnboardingIncomplete:
            out.append(
                Check(
                    name=name,
                    status="warn",
                    summary=f"{kind}.yaml not created yet",
                    fix="Ask your AI tool to run the jobhunt-setup skill (onboarding).",
                )
            )
        except ConfigError as e:
            out.append(
                Check(
                    name=name,
                    status="fail",
                    summary=e.message,
                    fix=e.next_actions[0] if e.next_actions else None,
                )
            )
    return out


def find_chrome() -> str | None:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    for exe in ("google-chrome", "google-chrome-stable", "chrome"):
        if found := shutil.which(exe):
            return found
    return None


def playwright_browsers_dir() -> Path:
    if env := os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return Path(env)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def check_browser() -> Check:
    if importlib.util.find_spec("playwright") is None:
        return Check(
            name="browser",
            status="warn",
            summary="Playwright not installed (needed for pre-fill, Milestone 4)",
            fix="Reinstall with the browser extra: `uv tool install 'jobhunt[browser]'`.",
        )
    chrome = find_chrome()
    if chrome:
        return Check(name="browser", status="ok", summary=f"Playwright + Chrome ({chrome})")
    bundled = list(playwright_browsers_dir().glob("chromium-*"))
    if bundled:
        return Check(name="browser", status="ok", summary="Playwright + bundled Chromium")
    return Check(
        name="browser",
        status="fail",
        summary="No Chrome or Playwright Chromium found",
        fix="Run `jobhunt doctor --fix` (installs Playwright Chromium).",
        fixable=True,
    )


def detect_ai_tools() -> dict[str, Path]:
    """Detected tool -> its user-level skills directory."""
    home = Path.home()
    tools: dict[str, Path] = {}
    if (home / ".claude").is_dir() or shutil.which("claude"):
        tools["claude-code"] = home / ".claude" / "skills"
    if (home / ".codex").is_dir() or shutil.which("codex"):
        tools["codex"] = home / ".agents" / "skills"
    return tools


def check_skills() -> list[Check]:
    tools = detect_ai_tools()
    if not tools:
        return [
            Check(
                name="skills",
                status="warn",
                summary="No supported AI tool detected",
                fix="Install Claude Code or Codex CLI, then run `jobhunt skills install`.",
            )
        ]
    out = []
    for tool, skills_dir in tools.items():
        present = [s for s in SKILL_NAMES if (skills_dir / s / "SKILL.md").exists()]
        name = f"skills.{tool}"
        if len(present) == len(SKILL_NAMES):
            out.append(
                Check(name=name, status="ok", summary=f"All {len(present)} skills in {skills_dir}")
            )
        else:
            out.append(
                Check(
                    name=name,
                    status="warn",
                    summary=f"{len(present)}/{len(SKILL_NAMES)} jobhunt skills for {tool}",
                    fix="Run `jobhunt skills install` (available from Milestone 5).",
                )
            )
    return out


def check_tracker(paths: Paths) -> Check:
    try:
        cfg = load_config(paths)
    except ConfigError:
        return Check(name="tracker", status="skip", summary="Skipped until config is valid")
    t = cfg.tracker
    if t.kind == "none":
        return Check(name="tracker", status="ok", summary="Tracker disabled (SQLite only)")
    if t.kind == "xlsx":
        target = tracker_xlsx_path(cfg, paths)
        parent = target.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if os.access(parent, os.W_OK):
            return Check(name="tracker", status="ok", summary=f"XLSX tracker at {target}")
        return Check(
            name="tracker",
            status="fail",
            summary=f"Cannot write to {target.parent}",
            fix=f"Set tracker.xlsx_path in {paths.config} to a writable location.",
        )
    sa = t.sheets.service_account_file
    if not t.sheets.spreadsheet_id or not sa:
        return Check(
            name="tracker",
            status="fail",
            summary="Google Sheets tracker is not configured",
            fix=f"Set tracker.sheets.spreadsheet_id and service_account_file in {paths.config}.",
        )
    if not Path(sa).expanduser().exists():
        return Check(
            name="tracker",
            status="fail",
            summary=f"Service account file not found: {sa}",
            fix="Download the service-account JSON key and point service_account_file at it.",
        )
    return Check(
        name="tracker",
        status="warn",
        summary="Sheets configured; live connectivity check arrives in Milestone 2",
    )


def check_sources(paths: Paths, offline: bool) -> list[Check]:
    if offline:
        return [Check(name="sources", status="skip", summary="Skipped (--offline)")]
    try:
        ua = load_config(paths).http.user_agent
    except ConfigError:
        from jobhunt import __version__

        ua = f"jobhunt/{__version__} (doctor)"
    out = []
    for label, url in SOURCE_PROBES.items():
        p = probe(url, user_agent=ua)
        if p.reachable:
            out.append(
                Check(name=f"source.{label}", status="ok", summary=f"{url} reachable ({p.detail})")
            )
        else:
            out.append(
                Check(
                    name=f"source.{label}",
                    status="warn",
                    summary=f"{url} {p.detail}",
                    fix="Check your network connection or proxy settings, then re-run doctor.",
                )
            )
    return out


def run_checks(paths: Paths, *, offline: bool) -> list[Check]:
    checks = [
        check_python(),
        check_install(),
        check_data_dir(paths),
        check_config(paths),
        check_store(paths),
    ]
    checks += check_onboarding(paths)
    checks.append(check_browser())
    checks += check_skills()
    checks.append(check_tracker(paths))
    checks += check_sources(paths, offline)
    return checks


# --- fixes ----------------------------------------------------------------------------------


def _fix_data_dir(paths: Paths, dry_run: bool) -> list[str]:
    missing = paths.missing_directories()
    if not dry_run:
        paths.ensure()
    return [f"create {d}" for d in missing]


def _fix_config(paths: Paths, dry_run: bool) -> list[str]:
    if paths.config.exists():
        return []
    if not dry_run:
        paths.home.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(package_file("defaults", "config.user.yaml"), paths.config)
        secure_file(paths.config)
    return [f"write starter {paths.config}"]


def _fix_store(paths: Paths, dry_run: bool) -> list[str]:
    if dry_run:
        if not paths.db.exists():
            return [f"create {paths.db} at schema v{SCHEMA_VERSION}"]
        conn = connect(paths.db)
        try:
            v = current_version(conn)
        finally:
            conn.close()
        return [f"migrate {paths.db} v{v} -> v{SCHEMA_VERSION}"] if v < SCHEMA_VERSION else []
    conn = connect(paths.db)
    try:
        return [f"apply migration {m}" for m in migrate(conn)]
    finally:
        conn.close()


def _fix_browser(paths: Paths, dry_run: bool) -> list[str]:
    if dry_run:
        return ["install Playwright Chromium"]
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=True,
        stdout=sys.stderr,
        stderr=sys.stderr,
    )
    return ["installed Playwright Chromium"]


FIXERS: dict[str, Callable[[Paths, bool], list[str]]] = {
    "data_dir": _fix_data_dir,
    "config": _fix_config,
    "store": _fix_store,
    "browser": _fix_browser,
}


def apply_fixes(paths: Paths, checks: list[Check], dry_run: bool) -> list[str]:
    done: list[str] = []
    # data_dir must come first: the store and config live inside it
    for name in FIXERS:
        if any(c.name == name and c.fixable and c.status != "ok" for c in checks):
            done += FIXERS[name](paths, dry_run)
    return done


def doctor(paths: Paths, *, offline: bool, fix: bool, dry_run: bool) -> DoctorReport:
    checks = run_checks(paths, offline=offline)
    fixed: list[str] = []
    if fix:
        fixed = apply_fixes(paths, checks, dry_run)
        if fixed and not dry_run:
            checks = run_checks(paths, offline=offline)
    counts = {s: sum(c.status == s for c in checks) for s in ("ok", "warn", "fail", "skip")}
    return DoctorReport(checks=checks, counts=counts, fixed=fixed, dry_run=dry_run)
