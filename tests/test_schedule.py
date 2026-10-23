"""`jobhunt schedule`: launchd, cron and Task Scheduler entries, with the OS commands faked."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from jobhunt import schedule as sch
from jobhunt.errors import ExitCode


class FakeOS:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.crontab: str | None = "MAILTO=me\n5 5 * * * /usr/bin/true\n"
        self.loaded = False
        self.task = False

    def __call__(self, args: list[str], stdin: str | None = None) -> sch.Proc:
        self.calls.append(args)
        if args[:2] == ["crontab", "-l"]:
            return sch.Proc(0, self.crontab) if self.crontab is not None else sch.Proc(1, "none")
        if args[:2] == ["crontab", "-"]:
            self.crontab = stdin
            return sch.Proc(0, "")
        if args[:2] == ["launchctl", "bootstrap"]:
            self.loaded = True
        if args[:2] == ["launchctl", "bootout"]:
            self.loaded = False
        if args[:2] == ["launchctl", "print"]:
            return sch.Proc(0 if self.loaded else 113, "")
        if args[:2] == ["schtasks", "/Create"]:
            self.task = True
        if args[:2] == ["schtasks", "/Delete"]:
            self.task = False
        if args[:2] == ["schtasks", "/Query"]:
            return sch.Proc(0 if self.task else 1, "")
        return sch.Proc(0, "")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOS:
    f = FakeOS()
    monkeypatch.setattr(sch, "run", f)
    return f


@pytest.fixture
def home(persona_home) -> Path:
    return persona_home("first-year-intern")


def test_cron_install_is_idempotent_and_keeps_other_lines(cli, home, fake):
    for _ in range(2):
        code, env, _ = cli("schedule", "install", "--platform", "cron")
        assert code == ExitCode.OK, env
    lines = fake.crontab.splitlines()
    assert lines[:2] == ["MAILTO=me", "5 5 * * * /usr/bin/true"]
    ours = [ln for ln in lines if sch.CRON_MARK in ln]
    assert len(ours) == 1 and ours[0].startswith("17 */4 * * * ")
    assert " run >> " in ours[0] and "JOBHUNT_HOME=" in ours[0]
    _, st, _ = cli("schedule", "status", "--platform", "cron")
    assert st["data"]["status"]["installed"] is True
    _, rm, _ = cli("schedule", "remove", "--platform", "cron")
    assert rm["data"]["action"] == "removed"
    assert fake.crontab.splitlines() == ["MAILTO=me", "5 5 * * * /usr/bin/true"]
    _, rm, _ = cli("schedule", "remove", "--platform", "cron")
    assert rm["data"]["action"] == "nothing_to_remove"


def test_cron_with_no_crontab_and_custom_interval(cli, home, fake):
    fake.crontab = None
    cli("schedule", "install", "--platform", "cron", "--every", "0.5")
    assert fake.crontab.startswith("*/30 * * * * ")


def test_launchd_plist(cli, home, fake):
    code, env, _ = cli("schedule", "install", "--platform", "launchd")
    assert code == ExitCode.OK
    path = Path(env["data"]["location"])
    assert path.is_relative_to(Path.home())
    pl = plistlib.loads(path.read_bytes())
    assert pl["Label"] == sch.LABEL and pl["StartInterval"] == 4 * 3600
    assert pl["ProgramArguments"][-1] == "run"
    assert Path(pl["StandardOutPath"]).parts[-2:] == ("logs", "run.log")
    _, st, _ = cli("schedule", "status", "--platform", "launchd")
    assert st["data"]["status"]["installed"] is True
    cli("schedule", "remove", "--platform", "launchd")
    assert not path.exists() and not fake.loaded


def test_windows_task(cli, home, fake):
    code, _env, _ = cli("schedule", "install", "--platform", "windows", "--every", "6")
    assert code == ExitCode.OK
    create = next(c for c in fake.calls if c[:2] == ["schtasks", "/Create"])
    assert create[create.index("/MO") + 1] == "6" and "/TN" in create
    script = (home / "run-scheduled.cmd").read_text(encoding="utf-8")
    assert "run >>" in script and "JOBHUNT_HOME" in script
    _, rm, _ = cli("schedule", "remove", "--platform", "windows")
    assert rm["data"]["action"] == "removed"


def test_dry_run_touches_nothing(cli, home, fake):
    code, env, _ = cli("schedule", "install", "--platform", "cron", "--dry-run")
    assert code == ExitCode.OK and sch.CRON_MARK in env["data"]["entry"]
    code, env, _ = cli("schedule", "install", "--platform", "launchd", "--dry-run")
    assert "<plist" in env["data"]["entry"]
    assert not any(c[:2] in (["crontab", "-"], ["launchctl", "bootstrap"]) for c in fake.calls)
    assert not sch.plist_path(Path.home()).exists()


def test_scheduler_failure_is_an_error(cli, home, monkeypatch):
    monkeypatch.setattr(sch, "run", lambda args, stdin=None: sch.Proc(1, "permission denied"))
    code, env, _ = cli("schedule", "install", "--platform", "cron")
    assert code != ExitCode.OK and "permission denied" in env["error"]["message"]


@pytest.mark.parametrize(
    ("platform", "words"),
    [("launchd", "once when it wakes"), ("cron", "skipped"), ("windows", "skipped")],
)
def test_install_and_status_say_it_runs_only_while_awake(cli, home, fake, platform, words):
    code, env, _ = cli("schedule", "install", "--platform", platform)
    assert code == ExitCode.OK, env
    note = env["data"]["runs_when"]
    assert "awake" in note and words in note and note in env["hints"]["user_actions"][0]
    _, st, _ = cli("schedule", "status", "--platform", platform)
    assert st["data"]["runs_when"] == note
