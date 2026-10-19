"""Digest, fast alerts and push channels (network mocked; nothing leaves the machine)."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

from jobhunt.errors import ExitCode
from jobhunt.models.config import SmtpConfig
from jobhunt.notify import channels as ch
from tests.conftest import PERSONAS_DIR, REPO

SRC = REPO / "tests" / "fixtures" / "sources"
OVERLAY = REPO / "tests" / "fixtures" / "forms" / "overlay"


@pytest.fixture
def world(tmp_path: Path) -> Path:
    root = tmp_path / "world"
    shutil.copytree(SRC / "base", root)
    shutil.copytree(OVERLAY, root, dirs_exist_ok=True)
    return root


@pytest.fixture
def posts(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    sent: list[dict] = []

    def fake(url: str, body: bytes, headers: dict[str, str]) -> int:
        sent.append({"url": url, "body": body.decode(), "headers": headers})
        return 200

    monkeypatch.setattr(ch, "http_post", fake)
    return sent


def _secrets(persona: str) -> list[str]:
    a = yaml.safe_load((PERSONAS_DIR / persona / "answers.yaml").read_text())
    ident = a.get("identity") or {}
    return [
        v for k in ("email", "phone", "legal_last_name", "address_line1") if (v := ident.get(k))
    ]


CFG = "notifications:\n  ntfy_topic: jh-test-topic\n  discord_webhook: https://discord.example/api/webhooks/1/x\n"


def test_digest_lists_what_waits_on_you_and_moves_on(cli, persona_home, world):
    home = persona_home("intl-sponsorship")
    (home / "config.yaml").write_text("fit:\n  priority_threshold: 0\n")  # read every form
    assert cli("run", "--fixtures", str(world), "--no-sync")[0] == ExitCode.OK
    code, env, _ = cli("digest")
    assert code == ExitCode.OK
    d = env["data"]["digest"]
    assert d["counts"]["ready"] >= 1 and d["counts"]["essays"] >= 1
    essay = d["sections"]["essays"][0]["text"]
    assert "Why do you want to work at" in essay
    assert d["text"].startswith("jobhunt digest") and "run my job hunt" in d["text"]
    for s in _secrets("intl-sponsorship"):
        assert s not in json.dumps(env)
    _, again, _ = cli("digest")
    assert again["data"]["digest"]["since"] == env["data"]["digest"]["until"]
    assert "NEW " not in again["data"]["digest"]["text"]


def test_run_pushes_alerts_once_and_the_digest_daily(cli, persona_home, world, posts):
    home = persona_home("first-year-intern")
    (home / "config.yaml").write_text(CFG + "  alert_priority: 0\n")
    code, env, _ = cli("run", "--fixtures", str(world), "--no-sync")
    assert code == ExitCode.OK
    n = env["data"]["notify"]
    assert n["alerts"] == []  # the first run is a silent baseline
    assert n["digest_pushed"] == {"ntfy": "sent", "discord": "sent"}
    first = len(posts)
    shutil.copytree(SRC / "update", world, dirs_exist_ok=True)
    code, env, _ = cli("run", "--fixtures", str(world), "--no-sync")
    n = env["data"]["notify"]
    assert n["alerts"], "new postings in the update should alert"
    assert n["digest_pushed"] == {}  # daily: already pushed today
    alerts = posts[first:]
    assert alerts and all(p["headers"].get("Priority", "high") == "high" for p in alerts
                          if "ntfy" in p["url"])  # fmt: skip
    assert posts[0]["url"] == "https://ntfy.sh/jh-test-topic"
    disc = next(p for p in posts if "discord" in p["url"])
    assert json.loads(disc["body"])["allowed_mentions"] == {"parse": []}
    before = len(posts)
    cli("run", "--fixtures", str(world), "--no-sync")
    assert len(posts) == before  # no repeat alerts, no second digest
    for p in posts:
        for s in _secrets("first-year-intern"):
            assert s not in p["body"]


def test_dry_run_and_no_notify_push_nothing(cli, persona_home, world, posts):
    home = persona_home("first-year-intern")
    (home / "config.yaml").write_text(CFG)
    cli("run", "--fixtures", str(world), "--no-sync", "--dry-run")
    cli("run", "--fixtures", str(world), "--no-sync", "--no-notify")
    assert posts == []


def test_failed_channel_is_reported(cli, persona_home, world, monkeypatch):
    home = persona_home("first-year-intern")
    (home / "config.yaml").write_text("notifications:\n  ntfy_topic: t\n")

    def down(url, body, headers):
        raise ch.NotifyError("URLError: offline")

    monkeypatch.setattr(ch, "http_post", down)
    code, env, _ = cli("run", "--fixtures", str(world), "--no-sync")
    assert code == ExitCode.OK
    assert any("couldn't reach ntfy" in a for a in env["hints"]["user_actions"])
    db = sqlite3.connect(home / "jobhunt.db")
    assert db.execute("SELECT status FROM notifications WHERE channel='ntfy'").fetchone()[0] == (
        "failed"
    )


def test_notify_test_and_missing_channels(cli, persona_home, posts):
    home = persona_home("first-year-intern")
    code, env, _ = cli("notify", "test")
    assert code == ExitCode.VALIDATION
    code, env, _ = cli("digest", "--push")
    assert code == ExitCode.VALIDATION
    (home / "config.yaml").write_text(CFG)
    code, env, _ = cli("notify", "test", "--dry-run")
    assert env["data"]["results"] == {"ntfy": "would send", "discord": "would send"} and not posts
    code, env, _ = cli("notify", "test")
    assert env["data"]["results"] == {"ntfy": "sent", "discord": "sent"} and len(posts) == 2


def test_own_ntfy_server_url():
    assert ch.Ntfy("https://ntfy.example/jobs", "ua").url == "https://ntfy.example/jobs"


def test_smtp_sends_only_to_you(monkeypatch):
    seen = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            seen["host"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            seen["tls"] = True

        def login(self, u, p):
            seen["login"] = (u, p)

        def send_message(self, msg):
            seen["to"], seen["subject"] = msg["To"], msg["Subject"]

    monkeypatch.setattr(ch.smtplib, "SMTP", FakeSMTP)
    cfg = SmtpConfig(host="smtp.example", username="me@example.com", password_env="JH_PW",
                     to="me@example.com")  # fmt: skip
    with pytest.raises(ch.NotifyError, match="JH_PW"):
        ch.Smtp(cfg).send("t", "x")
    monkeypatch.setenv("JH_PW", "pw")
    ch.Smtp(cfg).send("jobhunt digest", "body")
    assert seen == {"host": ("smtp.example", 587), "tls": True, "login": ("me@example.com", "pw"),
                    "to": "me@example.com", "subject": "jobhunt digest"}  # fmt: skip
