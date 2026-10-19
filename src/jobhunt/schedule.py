"""Run ``jobhunt run`` on a schedule with the OS's own scheduler: launchd, cron or Task Scheduler.

One job named ``jobhunt-run`` per user. It runs every ``schedule.interval_hours`` and logs to
``~/.jobhunt/logs/run.log``. Install, status and remove are idempotent; ``--dry-run`` shows the
exact entry without touching the system. Only the user's own scheduler entries are touched.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jobhunt.errors import JobhuntError
from jobhunt.paths import ENV_HOME, Paths

Kind = Literal["launchd", "cron", "windows"]
LABEL = "io.github.jobhunt.run"
TASK_NAME = "jobhunt-run"
CRON_MARK = "# jobhunt-run (managed by `jobhunt schedule`)"


@dataclass
class Proc:
    code: int
    out: str


def run(args: list[str], stdin: str | None = None) -> Proc:
    """Run a scheduler command. Replaced in tests."""
    try:
        p = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return Proc(127, str(e))
    return Proc(p.returncode, (p.stdout or "") + (p.stderr or ""))


class ScheduleError(JobhuntError):
    code = "schedule_failed"


def detect() -> Kind:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("win"):
        return "windows"
    return "cron"


def command() -> list[str]:
    """How the scheduler starts jobhunt: the installed script, else this Python with -m."""
    exe = shutil.which("jobhunt")
    if exe:
        return [str(Path(exe).resolve()), "run"]
    return [sys.executable, "-m", "jobhunt", "run"]


def _env(paths: Paths) -> dict[str, str]:
    exe_dir = str(Path(command()[0]).parent)
    env = {"PATH": os.pathsep.join([exe_dir, "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin",
                                    "/bin"])}  # fmt: skip
    if os.environ.get(ENV_HOME):
        env[ENV_HOME] = str(paths.home)
    return env


@dataclass
class Plan:
    kind: Kind
    location: str
    entry: str
    interval_hours: float


def launchd_plist(paths: Paths, hours: float) -> bytes:
    log = str(paths.logs / "run.log")
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": command(),
            "StartInterval": max(60, int(hours * 3600)),
            "RunAtLoad": False,
            "StandardOutPath": log,
            "StandardErrorPath": log,
            "EnvironmentVariables": _env(paths),
            "ProcessType": "Background",
        }
    )


def cron_line(paths: Paths, hours: float) -> str:
    if hours >= 1:
        when = f"17 */{max(1, round(hours))} * * *" if hours < 24 else "17 9 * * *"
    else:
        when = f"*/{max(5, round(hours * 60))} * * * *"
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in _env(paths).items())
    cmd = " ".join(shlex.quote(c) for c in command())
    log = shlex.quote(str(paths.logs / "run.log"))
    return f"{when} {env} {cmd} >> {log} 2>&1 {CRON_MARK}"


def windows_script(paths: Paths) -> str:
    """The .cmd file Task Scheduler runs (quoting a whole command line into /TR is fragile)."""
    sets = "".join(f'set "{k}={v}"\r\n' for k, v in _env(paths).items() if k != "PATH")
    log = str(paths.logs / "run.log")
    return f'@echo off\r\n{sets}{subprocess.list2cmdline(command())} >> "{log}" 2>&1\r\n'


def windows_script_path(paths: Paths) -> Path:
    return paths.home / "run-scheduled.cmd"


def windows_args(paths: Paths, hours: float) -> list[str]:
    if hours >= 1:
        sc = ["/SC", "HOURLY", "/MO", str(max(1, round(hours)))]
    else:
        sc = ["/SC", "MINUTE", "/MO", str(max(5, round(hours * 60)))]
    tr = f'"{windows_script_path(paths)}"'
    return ["schtasks", "/Create", "/F", "/TN", TASK_NAME, *sc, "/TR", tr]


def plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def plan(kind: Kind, paths: Paths, hours: float) -> Plan:
    if kind == "launchd":
        return Plan(kind, str(plist_path(Path.home())), launchd_plist(paths, hours).decode(), hours)
    if kind == "cron":
        return Plan(kind, "crontab", cron_line(paths, hours), hours)
    entry = subprocess.list2cmdline(windows_args(paths, hours)) + "\n" + windows_script(paths)
    return Plan(kind, f"Task Scheduler: {TASK_NAME}", entry, hours)


def _crontab() -> list[str]:
    p = run(["crontab", "-l"])
    if p.code != 0:  # "no crontab for user"
        return []
    return p.out.splitlines()


def _uid_domain() -> str:
    return f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/0"


def install(kind: Kind, paths: Paths, hours: float) -> Plan:
    p = plan(kind, paths, hours)
    paths.logs.mkdir(parents=True, exist_ok=True)
    if kind == "launchd":
        path = plist_path(Path.home())
        path.parent.mkdir(parents=True, exist_ok=True)
        run(["launchctl", "bootout", f"{_uid_domain()}/{LABEL}"])  # replace an older one
        path.write_bytes(launchd_plist(paths, hours))
        r = run(["launchctl", "bootstrap", _uid_domain(), str(path)])
        if r.code != 0:
            raise ScheduleError(f"launchctl bootstrap failed: {r.out.strip()}")
    elif kind == "cron":
        lines = [ln for ln in _crontab() if CRON_MARK not in ln]
        r = run(["crontab", "-"], stdin="\n".join([*lines, p.entry]) + "\n")
        if r.code != 0:
            raise ScheduleError(f"crontab failed: {r.out.strip()}")
    else:
        windows_script_path(paths).write_text(windows_script(paths), encoding="utf-8")
        r = run(windows_args(paths, hours))
        if r.code != 0:
            raise ScheduleError(f"schtasks failed: {r.out.strip()}")
    return p


def status(kind: Kind) -> dict[str, object]:
    if kind == "launchd":
        path = plist_path(Path.home())
        loaded = run(["launchctl", "print", f"{_uid_domain()}/{LABEL}"]).code == 0
        return {"installed": path.exists() and loaded, "file": str(path), "loaded": loaded}
    if kind == "cron":
        line = next((ln for ln in _crontab() if CRON_MARK in ln), None)
        return {"installed": line is not None, "entry": line}
    r = run(["schtasks", "/Query", "/TN", TASK_NAME])
    return {"installed": r.code == 0}


def remove(kind: Kind) -> bool:
    """Remove the entry. Returns True when there was one."""
    if kind == "launchd":
        path = plist_path(Path.home())
        had = path.exists()
        run(["launchctl", "bootout", f"{_uid_domain()}/{LABEL}"])
        path.unlink(missing_ok=True)
        return had
    if kind == "cron":
        lines = _crontab()
        keep = [ln for ln in lines if CRON_MARK not in ln]
        if len(keep) == len(lines):
            return False
        run(["crontab", "-"], stdin="\n".join(keep) + ("\n" if keep else ""))
        return True
    if run(["schtasks", "/Query", "/TN", TASK_NAME]).code != 0:
        return False
    run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
    return True
