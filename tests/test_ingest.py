"""`jobhunt ingest`: every quote and citation verified, rejections specific, bands recomputed."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from jobhunt.errors import ExitCode
from jobhunt.ingest.verify import clean_text, norm, quote_in
from tests.conftest import PERSONAS, REPO
from tests.judge_helpers import judge_all

BASE = str(REPO / "tests" / "fixtures" / "sources" / "base")

RICH = """We build logistics APIs used by retailers and adtech platforms.
Requirements
- 6+ years building backend services
- Strong Go experience
- Experience with Kafka
- Experience with Rust in production
Nice to have
- Terraform
Our on-call rotation is one week in six."""


def _setup(cli, persona_home, persona: str = "senior-backend", rich: bool = False) -> Path:
    home = persona_home(persona)
    (home / "config.yaml").write_text("fit:\n  priority_threshold: 0\n", encoding="utf-8")
    cli("run", "--fixtures", BASE, "--no-sync", "--no-inspect")
    if rich:
        db = sqlite3.connect(home / "jobhunt.db")
        db.execute("UPDATE jobs SET description=? WHERE id=1", (RICH,))
        db.commit()
        db.close()
        cli("match")
    return home


def _task(home: Path, job_id: int) -> dict:
    [f] = (home / "work" / "tasks").glob(f"req-{job_id}-*.json")
    return json.loads(f.read_text(encoding="utf-8"))


def _write(task: dict, result: dict) -> Path:
    p = Path(task["result_path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result), encoding="utf-8")
    return p


def _good(task: dict) -> dict:
    lines = {ln["ref"]: ln["text"] for ln in task["profile_lines"]}
    by_text = {r["text"]: r["id"] for r in task["requirements"]}
    go_ref = next(r for r, t in lines.items() if "Go," in t or "Go " in t)
    reqs = []
    for text, rid in by_text.items():
        if "Go experience" in text:
            reqs.append(
                {
                    "id": rid,
                    "text": text,
                    "status": "met",
                    "tier": "stated",
                    "evidence_ref": go_ref,
                    "evidence_quote": lines[go_ref],
                }
            )
        elif "Rust" in text:
            reqs.append({"id": rid, "text": text, "status": "unmet", "tier": "none"})
        else:
            reqs.append(
                {
                    "id": rid,
                    "text": text,
                    "status": "partial",
                    "tier": "inferred",
                    "note": "close to listed work",
                }
            )
    return {
        "task_id": task["task_id"],
        "job_id": task["job"]["id"],
        "requirements": reqs,
        "gates": [
            {
                "gate": "avoid_industries",
                "result": "pass",
                "quote": "used by retailers and adtech platforms",
            }
        ],
        "dealbreakers": [{"dealbreaker": d, "hit": False} for d in task["dealbreakers"]],
        "narrative": {
            "reasons": [
                {"text": "Owns a Go API", "evidence_ref": go_ref, "evidence_quote": lines[go_ref]}
            ],
            "gaps": ["No Rust"],
            "pitch": "Go and Kafka at scale.",
            "why_wrong": "Rust may matter more than it reads.",
        },
    }


def test_normalization_is_exact_not_fuzzy():
    assert quote_in("“Strong   Go” experience", 'Must have "strong go" experience.')
    assert quote_in("Go​ experience", "strong go experience")
    assert not quote_in("Go experiense", "strong go experience")
    assert not quote_in("ab", "abc")  # too short to mean anything
    assert norm("A—B") == "a-b"
    assert clean_text("hi​‮there\U000e0041") == "hithere"


def test_rich_task_has_requirements_gate_and_dealbreakers(cli, persona_home):
    home = _setup(cli, persona_home, rich=True)
    t = _task(home, 1)
    assert [g["gate"] for g in t["unclear_gates"]] == ["avoid_industries"]
    assert len(t["requirements"]) >= 4 and t["dealbreakers"]


def test_valid_result_is_accepted_and_band_stops_being_provisional(cli, persona_home):
    home = _setup(cli, persona_home, rich=True)
    _, before, _ = cli("show", "1")
    assert before["data"]["fit"]["provisional"] is True
    task = _task(home, 1)
    _write(task, _good(task))
    code, env, _ = cli("ingest", "--task", task["task_id"])
    assert code == ExitCode.OK, env
    assert [a["task_id"] for a in env["data"]["accepted"]] == [task["task_id"]]
    assert env["data"]["refit"]["1"]["provisional"] is False
    _, shown, _ = cli("show", "1")
    fit = shown["data"]["fit"]
    assert fit["provisional"] is False and fit["band"] == env["data"]["refit"]["1"]["band"]
    assert fit["narrative"]["gaps"] == ["No Rust"]
    reqs = [r for r in shown["data"]["requirements"] if r["source"] == "ai"]
    go = next(r for r in reqs if "Go experience" in r["text"])
    assert go["tier"] == "stated" and go["evidence"]
    gate = next(g for g in shown["data"]["gates"] if g["gate"] == "avoid_industries")
    assert gate["result"] == "pass" and "judged" in gate["reason"]
    # result and task files are archived; a re-run has nothing to do
    assert not Path(task["result_path"]).exists()
    assert (home / "work" / "tasks" / "ingested" / f"{task['task_id']}.json").exists()
    code, env, _ = cli("ingest")
    assert code == ExitCode.OK and env["data"]["accepted"] == []
    # unchanged posting + profile: match keeps the judgment and writes no new task
    _, m, _ = cli("match")
    assert m["data"]["tasks"]["written"] == 0
    _, shown2, _ = cli("show", "1")
    assert shown2["data"]["fit"]["provisional"] is False


@pytest.mark.parametrize(
    ("mutate", "path", "words"),
    [
        (
            lambda r: r["requirements"][0].update(text="10+ years of Haskell"),
            "$.requirements[0].text",
            "not found in the posting",
        ),
        (
            lambda r: r["narrative"]["reasons"][0].update(evidence_quote="Led 40 engineers"),
            "$.narrative.reasons[0].evidence_quote",
            "not found in profile line",
        ),
        (
            lambda r: r["narrative"]["reasons"][0].update(evidence_ref="experience[9]"),
            "$.narrative.reasons[0].evidence_ref",
            "not a profile line",
        ),
        (lambda r: r["requirements"].pop(), "$.requirements", "have no verdict"),
        (lambda r: r.update(gates=[]), "$.gates", "avoid_industries"),
        (
            lambda r: r["gates"][0].update(quote="we never mention ads"),
            "$.gates[0].quote",
            "not found",
        ),
        (lambda r: r.update(dealbreakers=[]), "$.dealbreakers", "check dealbreaker"),
        (
            lambda r: r["dealbreakers"][0].update(hit=True),
            "$.dealbreakers[0].quote",
            "exact posting quote",
        ),
        (
            lambda r: next(q for q in r["requirements"] if q["tier"] == "none").update(
                status="met"
            ),
            "status",
            "tier none",
        ),
        (
            lambda r: next(q for q in r["requirements"] if q["tier"] == "inferred").update(note=""),
            "note",
            "explain",
        ),
        (
            lambda r: r["requirements"].append(
                {**r["requirements"][0], "id": None, "importance": "must", "type": "skill"}
            ),
            "id",
            "extract_requirements is false",
        ),
        (lambda r: r.update(job_id=99), "$.job_id", "must be 1"),
        (
            lambda r: r["requirements"][0].update(status="maybe"),
            "$.requirements[0].status",
            "Input should be",
        ),
    ],
)
def test_bad_results_are_rejected_with_specific_errors(cli, persona_home, mutate, path, words):
    home = _setup(cli, persona_home, rich=True)
    task = _task(home, 1)
    res = _good(task)
    mutate(res)
    f = _write(task, res)
    code, env, _ = cli("ingest")
    assert code == ExitCode.VALIDATION and env["ok"] is False
    [rej] = env["data"]["rejected"]
    assert any(path in e["path"] and words in e["message"] for e in rej["errors"]), rej["errors"]
    assert rej["attempts"] == 1 and f.exists()
    errors = json.loads((f.parent / f"{task['task_id']}.errors.json").read_text(encoding="utf-8"))
    assert errors["errors"] == rej["errors"]
    _, shown, _ = cli("show", "1")
    assert shown["data"]["fit"]["provisional"] is True
    assert not [r for r in shown["data"]["requirements"] if r["source"] == "ai"]
    # still pending, attempts counted
    _, tasks, _ = cli("tasks", "--job", "1")
    assert tasks["data"]["tasks"][0]["attempts"] == 1
    assert tasks["data"]["tasks"][0]["last_errors"] == rej["errors"]


def test_stated_needs_the_line_to_name_the_skill(cli, persona_home):
    home = _setup(cli, persona_home, rich=True)
    task = _task(home, 1)
    res = _good(task)
    rust = next(q for q in res["requirements"] if "Rust" in q["text"])
    go = next(q for q in res["requirements"] if q["tier"] == "stated")
    rust.update(
        status="met",
        tier="stated",
        evidence_ref=go["evidence_ref"],
        evidence_quote=go["evidence_quote"],
    )
    _write(task, res)
    _, env, _ = cli("ingest")
    [rej] = env["data"]["rejected"]
    assert any("doesn't name rust" in e["message"].lower() for e in rej["errors"]), rej


def test_fix_after_rejection_then_accept(cli, persona_home):
    home = _setup(cli, persona_home, rich=True)
    task = _task(home, 1)
    bad = _good(task)
    bad["requirements"][0]["text"] = "invented requirement"
    f = _write(task, bad)
    assert cli("ingest")[0] == ExitCode.VALIDATION
    _write(task, _good(task))
    code, env, _ = cli("ingest")
    assert code == ExitCode.OK and env["data"]["accepted"]
    assert not (f.parent / f"{task['task_id']}.errors.json").exists()


def test_malformed_and_unknown_files(cli, persona_home):
    home = _setup(cli, persona_home)
    res = home / "work" / "results"
    (res / "req-1-nope.json").write_text("{not json", encoding="utf-8")
    (res / "ghost.json").write_text(json.dumps({"task_id": "ghost"}), encoding="utf-8")
    (res / "mismatch.json").write_text(json.dumps({"task_id": "other"}), encoding="utf-8")
    code, env, _ = cli("ingest")
    assert code == ExitCode.VALIDATION
    msgs = {r["task_id"]: r["errors"][0]["message"] for r in env["data"]["rejected"]}
    assert "not valid JSON" in msgs["req-1-nope"]
    assert "file name" in msgs["mismatch"]
    assert env["data"]["skipped"][0]["reason"] == "no task 'ghost'"


def test_dealbreaker_hit_gates_the_job_out(cli, persona_home):
    home = _setup(cli, persona_home, rich=True)
    task = _task(home, 1)
    res = _good(task)
    res["dealbreakers"][1].update(hit=True, quote="on-call rotation is one week in six")
    _write(task, res)
    assert cli("ingest")[0] == ExitCode.OK
    _, shown, _ = cli("show", "1")
    assert shown["data"]["gate_status"] == "failed"
    g = next(g for g in shown["data"]["gates"] if g["gate"] == "dealbreaker")
    assert "On-call" in g["reason"] and g["evidence"]


def test_ai_gate_fail_gates_the_job_out(cli, persona_home):
    home = _setup(cli, persona_home, rich=True)
    task = _task(home, 1)
    res = _good(task)
    res["gates"][0]["result"] = "fail"
    _write(task, res)
    assert cli("ingest")[0] == ExitCode.OK
    _, env, _ = cli("jobs", "--gated")
    assert 1 in [j["id"] for j in env["data"]["jobs"]]


def test_changed_posting_drops_the_judgment_and_writes_a_new_task(cli, persona_home):
    home = _setup(cli, persona_home, rich=True)
    task = _task(home, 1)
    _write(task, _good(task))
    cli("ingest")
    db = sqlite3.connect(home / "jobhunt.db")
    db.execute("UPDATE jobs SET description=? WHERE id=1", (RICH + "\n- Experience with Elixir",))
    db.commit()
    db.close()
    _, m, _ = cli("match")
    assert m["data"]["tasks"]["written"] >= 1
    _, shown, _ = cli("show", "1")
    assert shown["data"]["fit"]["provisional"] is True
    assert not [r for r in shown["data"]["requirements"] if r["source"] == "ai"]
    assert _task(home, 1)["task_id"] != task["task_id"]


def test_hidden_unicode_is_stripped_from_stored_text(cli, persona_home):
    home = _setup(cli, persona_home, rich=True)
    task = _task(home, 1)
    res = _good(task)
    res["narrative"]["pitch"] = "Go​ and Kafka‮ at scale\U000e0049"
    _write(task, res)
    cli("ingest")
    _, shown, _ = cli("show", "1")
    assert shown["data"]["fit"]["narrative"]["pitch"] == "Go and Kafka at scale"


def test_dry_run_writes_nothing(cli, persona_home):
    home = _setup(cli, persona_home, rich=True)
    task = _task(home, 1)
    f = _write(task, _good(task))
    before = (home / "jobhunt.db").read_bytes()
    code, env, _ = cli("ingest", "--dry-run")
    assert code == ExitCode.OK and env["data"]["accepted"]
    assert (home / "jobhunt.db").read_bytes() == before and f.exists()


@pytest.mark.parametrize("persona", PERSONAS)
def test_every_persona_judged_end_to_end(cli, persona_home, persona):
    home = _setup(cli, persona_home, persona)
    _, listed, _ = cli("tasks")
    ids = judge_all(home)
    assert ids and len(ids) == listed["data"]["by_kind"]["requirement_evidence"]
    code, env, _ = cli("ingest")
    assert code == ExitCode.OK, env["data"]["rejected"]
    assert len(env["data"]["accepted"]) == len(ids)
    assert env["data"]["pending_tasks"] == 0
    _, tasks, _ = cli("tasks")
    assert tasks["data"]["tasks"] == []
    for jid, fit in env["data"]["refit"].items():
        _, shown, _ = cli("show", jid)
        user_unclear = [g for g in shown["data"]["gates"] if g["result"] == "unclear"]
        if fit["band"] is not None and not user_unclear:
            assert fit["provisional"] is False, (jid, fit)
