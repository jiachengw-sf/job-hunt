"""`jobhunt research` + ingest: budgets, 25-word quotes re-checked on their pages, 14-day cache."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from jobhunt.errors import ExitCode
from tests.conftest import REPO

BASE = str(REPO / "tests" / "fixtures" / "sources" / "base")
PAGES = str(REPO / "tests" / "fixtures" / "research")
ABOUT = "https://bluefin.example/about"
NEWS = "https://news.example/2026/bluefin-series-b"


@pytest.fixture
def home(cli, persona_home) -> Path:
    h = persona_home("first-year-intern")
    cli("run", "--fixtures", BASE, "--no-sync", "--no-inspect")
    return h


def _task(cli) -> dict:
    code, env, _ = cli("research", "Bluefin Analytics")
    assert code == ExitCode.OK, env
    return json.loads(Path(env["data"]["task_path"]).read_text())


def _good(task: dict) -> dict:
    py = next(ln for ln in task["profile_lines"] if "Python" in ln["text"])
    return {
        "task_id": task["task_id"],
        "company": task["company"],
        "searches_used": 2,
        "fetches_used": 2,
        "sources": [ABOUT, NEWS],
        "what_they_do": [
            {
                "text": "Churn forecasting software for subscription businesses.",
                "url": ABOUT,
                "quote": "builds churn forecasting software for subscription businesses",
            }
        ],
        "size": [
            {"text": "About 120 people.", "url": ABOUT, "quote": "a team of about 120 people"}
        ],
        "stage_funding": [
            {
                "text": "Series B, $40M.",
                "url": NEWS,
                "quote": "raised a $40 million Series B led by Harbor Ventures",
                "date": "2026-03",
            }
        ],
        "recent_news": [
            {
                "text": "Raised a Series B.",
                "url": NEWS,
                "quote": "Bluefin Analytics raises Series B",
                "date": "2026-03",
            }
        ],
        "early_career": [
            {
                "text": "Summer intern cohort with return offers.",
                "url": ABOUT,
                "quote": "last year 70% received return offers",
            }
        ],
        "user_angle": [
            {
                "text": "Uses Python, as the team does.",
                "evidence_ref": py["ref"],
                "evidence_quote": py["text"],
                "fact_url": ABOUT,
            }
        ],
        "not_found": ["interview_process"],
    }


def _write(task: dict, res: dict) -> None:
    Path(task["result_path"]).write_text(json.dumps(res))


def test_task_has_budget_rules_and_no_personal_data(cli, home):
    t = _task(cli)
    assert t["budget"] == {"max_searches": 5, "max_fetches": 8}
    assert "subagents" in t["instructions"] and "25 words" in t["instructions"]
    assert t["known"].startswith("<untrusted_content")
    text = json.dumps(t)
    assert "555-01" not in text and "@" not in text.replace("@context", "")
    _, again, _ = cli("research", "bluefin analytics")
    assert again["data"]["status"] == "pending" and again["data"]["task_id"] == t["task_id"]


def test_valid_brief_is_verified_on_its_pages_and_cached(cli, home):
    t = _task(cli)
    _write(t, _good(t))
    code, env, _ = cli("ingest", "--fixtures", PAGES)
    assert code == ExitCode.OK, env
    assert env["data"]["accepted"][0]["summary"]["facts"] == 5
    code, env, _ = cli("research", "Bluefin Analytics")
    assert env["data"]["status"] == "cached"
    b = env["data"]["brief"]
    assert b["summary"].startswith("Churn forecasting")
    assert b["user_angle"][0]["evidence_quote"]
    assert b["budget_used"] == {"searches": 2, "fetches": 2}
    _, sync, _ = cli("sync")
    assert sync["ok"]


@pytest.mark.parametrize(
    ("mutate", "path", "words"),
    [
        (lambda r: r.update(searches_used=6), "$.searches_used", "over budget"),
        (lambda r: r.update(fetches_used=9), "$.fetches_used", "over budget"),
        (lambda r: r.update(subagents_used=True), "$.subagents_used", "subagents"),
        (
            lambda r: r["size"][0].update(quote=" ".join(["word"] * 26)),
            "$.size[0].quote",
            "26 words",
        ),
        (
            lambda r: r["size"][0].update(url="https://elsewhere.example/x"),
            "$.size[0].url",
            "not in sources",
        ),
        (
            lambda r: r["size"][0].update(quote="a team of about 900 people"),
            "$.size[0].quote",
            "not found on",
        ),
        (lambda r: r["recent_news"][0].pop("date"), "$.recent_news[0].date", "needs a date"),
        (
            lambda r: r["user_angle"][0].update(evidence_quote="Led a team of 30"),
            "$.user_angle[0].evidence_quote",
            "not found in profile line",
        ),
        (lambda r: r.update(company="Other Co"), "$.company", "must be"),
        (
            lambda r: r["early_career"][0].update(
                quote="this company is the best employer in the world"
            ),
            "$.early_career[0].quote",
            "not found on",
        ),
    ],
)
def test_bad_briefs_are_rejected(cli, home, mutate, path, words):
    t = _task(cli)
    res = _good(t)
    mutate(res)
    _write(t, res)
    code, env, _ = cli("ingest", "--fixtures", PAGES)
    assert code == ExitCode.VALIDATION
    [rej] = env["data"]["rejected"]
    assert any(e["path"] == path and words in e["message"] for e in rej["errors"]), rej["errors"]


def test_facts_from_unreachable_pages_are_dropped_not_kept(cli, home):
    t = _task(cli)
    res = _good(t)
    gone = "https://news.example/2026/deleted"
    res["sources"].append(gone)
    res["fetches_used"] = 3
    res["engineering"] = [{"text": "Uses Rust.", "url": gone, "quote": "we love Rust"}]
    _write(t, res)
    code, env, _ = cli("ingest", "--fixtures", PAGES)
    assert code == ExitCode.OK
    assert env["data"]["accepted"][0]["summary"]["dropped_unreachable"] == 1
    _, b, _ = cli("research", "Bluefin Analytics")
    assert b["data"]["brief"]["engineering"] == []


def test_what_they_do_must_be_verifiable(cli, home):
    t = _task(cli)
    res = _good(t)
    gone = "https://bluefin.example/missing"
    res["sources"] = [gone, NEWS]
    res["what_they_do"][0]["url"] = gone
    for f in ("size", "early_career", "user_angle"):
        res.pop(f)
    _write(t, res)
    code, env, _ = cli("ingest", "--fixtures", PAGES)
    assert code == ExitCode.VALIDATION
    assert "could be loaded" in env["data"]["rejected"][0]["errors"][0]["message"]


def test_cache_expires_after_cache_days_and_refresh(cli, home):
    t = _task(cli)
    _write(t, _good(t))
    cli("ingest", "--fixtures", PAGES)
    _, env, _ = cli("research", "Bluefin Analytics", "--refresh")
    assert env["data"]["status"] == "written"
    db = sqlite3.connect(home / "jobhunt.db")
    db.execute("UPDATE tasks SET status='expired' WHERE kind='company_research'")
    db.execute("UPDATE companies SET brief_at='2026-01-01T00:00:00Z'")
    db.commit()
    db.close()
    _, env, _ = cli("research", "--job", "1")
    assert env["data"]["company"] == "Bluefin Analytics" and env["data"]["status"] == "written"
