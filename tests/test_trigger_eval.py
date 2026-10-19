"""The trigger-eval runner, offline: query file shape, stream parsing and scoring."""

from __future__ import annotations

import json

from jobhunt.devtools import trigger_eval as te
from jobhunt.skills import skill_names


def test_query_file_covers_every_skill_with_near_misses():
    cases = te.load_cases()
    for name in skill_names():
        mine = [c for c in cases if c.skill == name]
        pos = [c for c in mine if c.expect]
        assert 18 <= len(mine) <= 24, name
        assert abs(len(pos) - (len(mine) - len(pos))) <= 2, name
    apply_pos = [c for c in cases if c.skill == "jobhunt-apply" and c.expect]
    assert apply_pos and all(c.explicit for c in apply_pos)  # apply runs only when asked by name


def _line(**d) -> str:
    return json.dumps(d)


def test_parse_claude_stream():
    r = te.Run()
    assert not te.parse_claude_line(_line(type="system", subtype="init"), r)
    rl = {"rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization": 0.3}}}}
    te.parse_claude_line(_line(type="rate_limit_event", **rl), r)
    assert r.utilization == 0.3
    msg = {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "jobhunt-daily"}}]}
    assert te.parse_claude_line(_line(type="assistant", message=msg), r)
    assert r.loaded == ["jobhunt-daily"]
    r2 = te.Run()
    assert te.parse_claude_line(
        _line(type="assistant", message={"content": [{"type": "text", "text": "Hello"}]}), r2
    )
    assert r2.loaded == [] and r2.text == "Hello"


def test_parse_codex_stream():
    r = te.Run()
    line = _line(type="item.completed", item={"type": "command_execution",
                 "command": "cat /tmp/x/.agents/skills/jobhunt-judge/SKILL.md"})  # fmt: skip
    assert te.parse_codex_line(line, r) and r.loaded == ["jobhunt-judge"]


def test_scoring_and_explicit_invocation():
    cases = [
        te.Case("jobhunt-daily", "run my job hunt", True),
        te.Case("jobhunt-daily", "weather?", False),
        te.Case("jobhunt-apply", "/jobhunt-apply", True, explicit=True),
    ]
    yes = te.Run(loaded=["jobhunt-daily"])
    no = te.Run(text="It is sunny.")
    applied = te.Run(text="First I'll run `jobhunt prefill --dry-run`.")
    runs = {0: [yes, yes, no], 1: [no, no, yes], 2: [applied, applied, applied]}
    s = te.score(cases, runs)
    assert s["jobhunt-daily"]["passed"] == 2 and s["jobhunt-daily"]["pass"]
    assert s["jobhunt-apply"]["passed"] == 1
    runs[0] = [yes, no, no]
    assert te.score(cases, runs)["jobhunt-daily"]["failures"][0]["correct_runs"] == 1
    assert te.tool_query("codex", cases[2]) == "$jobhunt-apply"
