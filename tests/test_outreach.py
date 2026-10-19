"""Outreach: referral and follow-up drafts the user sends; follow-up timing and caps."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import jobhunt.cli.main  # noqa: F401  (registers every command)
from jobhunt.cli.framework import REGISTRY
from jobhunt.errors import ExitCode
from tests.conftest import REPO

BASE = str(REPO / "tests" / "fixtures" / "sources" / "base")


@pytest.fixture
def home(cli, persona_home) -> Path:
    h = persona_home("first-year-intern")
    (h / "config.yaml").write_text("fit:\n  priority_threshold: 0\n")
    cli("run", "--fixtures", BASE, "--no-sync", "--no-inspect")
    return h


def _task(cli, *args: str) -> dict:
    code, env, _ = cli("outreach", "draft", *args)
    assert code == ExitCode.OK, env
    return json.loads(Path(env["data"]["task_path"]).read_text())


def _good(task: dict) -> dict:
    line = next(s for s in task["sources"] if s["ref"].startswith("projects"))
    return {
        "task_id": task["task_id"],
        "job_id": task["job"]["id"],
        "kind": task["message_kind"],
        "subject": f"{task['job']['title']} application",
        "sentences": [
            {"text": "Hi there,", "source": None},
            {"text": f"I applied for the {task['job']['title']} role.", "source": "posting",
             "quote": task["job"]["title"]},
            {"text": line["text"] + ".", "source": line["ref"]},
            {"text": "Thanks for your time.", "source": None, "new_paragraph": True},
        ],
    }  # fmt: skip


def _ingest(cli, task: dict, res: dict) -> tuple[int, dict]:
    Path(task["result_path"]).write_text(json.dumps(res))
    code, env, _ = cli("ingest")
    return code, env


def _age_sent(home: Path) -> None:
    db = sqlite3.connect(home / "jobhunt.db")
    db.execute("UPDATE drafts SET sent_at='2020-02-01T00:00:00Z' WHERE status='sent'")
    db.commit()
    db.close()


def test_due_lists_followups_and_referral_questions(cli, home):
    cli("mark", "1", "Applied", "--applied-on", "2020-01-01")
    cli("mark", "2", "Applied")  # today: not due yet
    code, env, _ = cli("outreach", "due")
    assert code == ExitCode.OK
    assert [f["job_id"] for f in env["data"]["followups"]] == [1]
    refs = env["data"]["referrals"]
    assert refs and all(r["question"].startswith("Know anyone at") for r in refs)
    assert 1 not in [r["job_id"] for r in refs]  # already applied
    assert env["hints"]["user_actions"]


def test_followup_cycle_respects_timing_and_the_cap(cli, home):
    cli("mark", "1", "Applied", "--applied-on", "2020-01-01")
    for n in (1, 2):
        t = _task(cli, "1", "--kind", "followup")
        assert t["application"]["followups_sent"] == n - 1
        code, env = _ingest(cli, t, _good(t))
        assert code == ExitCode.OK, env
        s = env["data"]["accepted"][0]["summary"]
        assert s["key"] == f"followup-{n}"
        body = Path(s["file"]).read_text()
        assert "jobhunt never sends anything" in body
        code, sent, _ = cli("outreach", "mark-sent", "1", "--kind", "followup")
        assert code == ExitCode.OK and sent["data"]["key"] == f"followup-{n}"
        if n == 1:  # just sent: next one isn't due yet
            code, env, _ = cli("outreach", "draft", "1", "--kind", "followup")
            assert code == ExitCode.VALIDATION and "due on" in env["error"]["message"]
            _age_sent(home)
    _age_sent(home)
    code, env, _ = cli("outreach", "draft", "1", "--kind", "followup")
    assert code == ExitCode.VALIDATION and "max 2" in env["error"]["message"]
    _, due, _ = cli("outreach", "due")
    assert due["data"]["followups"] == []


def test_followup_needs_an_application(cli, home):
    code, env, _ = cli("outreach", "draft", "3", "--kind", "followup")
    assert code == ExitCode.VALIDATION and "awaiting a response" in env["error"]["message"]


def test_referral_draft_is_grounded_and_short(cli, home):
    t = _task(cli, "1", "--kind", "referral", "--to", "a friend from the robotics club")
    assert t["recipient"] == "a friend from the robotics club" and t["max_chars"] == 600
    bad = _good(t)
    bad["sentences"][2]["text"] = "I led a team of 30 engineers."
    code, env = _ingest(cli, t, bad)
    assert code == ExitCode.VALIDATION
    msgs = " ".join(e["message"] for e in env["data"]["rejected"][0]["errors"])
    assert "number(s) 30" in msgs and "leadership" in msgs
    long = _good(t)
    long["sentences"] += [{"text": "Thanks again. " * 10, "source": None}] * 5
    code, env = _ingest(cli, t, long)
    assert "keep it under 600" in env["data"]["rejected"][0]["errors"][0]["message"]
    code, env = _ingest(cli, t, _good(t))
    assert code == ExitCode.OK
    _, shown, _ = cli("outreach", "show", "1")
    d = shown["data"]["drafts"][0]
    assert d["kind"] == "referral" and d["status"] == "draft" and shown["hints"]["user_actions"]
    _, due, _ = cli("outreach", "due")
    assert 1 not in [r["job_id"] for r in due["data"]["referrals"]]


def test_there_is_no_send_code_path():
    cmds = [n for n in REGISTRY if any(w in n for w in ("send", "sent", "submit", "post"))]
    assert cmds == ["outreach mark-sent"]
    for f in [*(REPO / "src/jobhunt/drafts").glob("*.py"), REPO / "src/jobhunt/cli/outreach.py",
              REPO / "src/jobhunt/ingest/outreach.py"]:  # fmt: skip
        text = f.read_text()
        for banned in ("smtplib", "urllib", "jobhunt.http", "webbrowser", "subprocess"):
            assert banned not in text, f"{banned} in {f.name}"
