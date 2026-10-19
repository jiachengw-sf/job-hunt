"""Skill trigger evals: does each skill load when it should, and stay out when it shouldn't?

``evals/triggers/queries.yaml`` holds ~20 queries per skill (half near-misses). Each runs N times
(default 3) in a scratch project where ``jobhunt skills install --scope project`` put the skills,
through the tool's headless mode with only its skill tool enabled, so nothing else can run.

    uv run python -m jobhunt.devtools.trigger_eval --tool claude-code
    uv run python -m jobhunt.devtools.trigger_eval --tool codex

A query passes when at least 2 of its 3 runs behave as expected; a skill passes when 90% of its
queries pass. Results go to ``evals/triggers/results/<tool>.json`` and ``.md``. Runs stop early
once the decision is visible, and the whole eval stops if the tool reports its usage window above
``--max-utilization``.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from jobhunt import skills as sk

REPO = Path(__file__).resolve().parents[3]
QUERIES = REPO / "evals" / "triggers" / "queries.yaml"
RESULTS = REPO / "evals" / "triggers" / "results"
QUERY_PASS = 2  # of 3 runs
SKILL_PASS = 0.9
_BASE_DIR = re.compile(r"Base directory for this skill: \S*?skills/([a-z0-9-]+)")
# An explicitly invoked skill is expanded into the prompt; its effect shows in the reply.
EXPLICIT_SIGNS = {"jobhunt-apply": re.compile(r"jobhunt prefill|pre-?fill", re.I)}


@dataclass
class Case:
    skill: str
    query: str
    expect: bool
    explicit: bool = False


@dataclass
class Run:
    loaded: list[str] = field(default_factory=list)
    text: str = ""
    utilization: float | None = None
    error: str | None = None
    seconds: float = 0.0


def load_cases(path: Path = QUERIES, only: set[str] | None = None) -> list[Case]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = []
    for skill, groups in data["skills"].items():
        if only and skill not in only:
            continue
        for expect, key in ((True, "trigger"), (False, "near_miss")):
            for q in groups[key]:
                if isinstance(q, dict):
                    out.append(Case(skill, q["query"], expect, bool(q.get("explicit"))))
                else:
                    out.append(Case(skill, q, expect))
    return out


def parse_claude_line(line: str, run: Run) -> bool:
    """Update ``run`` from one stream-json line. True once the decision is known."""
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return False
    t = d.get("type")
    if t == "rate_limit_event":
        u = (d.get("rate_limit_info") or {}).get("unifiedWindows", {}).get("five_hour", {})
        if "utilization" in u:
            run.utilization = float(u["utilization"])
    elif t == "assistant":
        for c in d.get("message", {}).get("content", []):
            if c.get("type") == "tool_use" and c.get("name") == "Skill":
                run.loaded.append(str(c.get("input", {}).get("skill", "")))
                return True
            if c.get("type") == "text" and c.get("text", "").strip():
                run.text = c["text"][:600]
                return True
    elif t == "user":
        m = _BASE_DIR.search(json.dumps(d))
        if m:
            run.loaded.append(m.group(1))
            return True
    elif t == "result":
        return True
    return False


def parse_codex_line(line: str, run: Run) -> bool:
    """Codex `exec --json` events: a skill counts as loaded when its SKILL.md is read."""
    for m in re.finditer(r"\.agents/skills/([a-z0-9-]+)/SKILL\.md", line):
        run.loaded.append(m.group(1))
        return True
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return False
    item = d.get("item") or {}
    if item.get("type") in ("agent_message", "assistant_message") and item.get("text"):
        run.text = str(item["text"])[:600]
        return True
    return d.get("type") in ("turn.completed", "turn.failed")


def claude_cmd(query: str, model: str | None) -> list[str]:
    cmd = ["claude", "-p", query, "--output-format", "stream-json", "--verbose",
           "--tools", "Skill", "--strict-mcp-config", "--no-session-persistence"]  # fmt: skip
    return cmd + (["--model", model] if model else [])


def codex_cmd(query: str, model: str | None) -> list[str]:
    cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only", query]
    return cmd + (["--model", model] if model else [])


def run_once(tool: str, query: str, cwd: Path, model: str | None, timeout: float = 120) -> Run:
    run = Run()
    start = time.monotonic()
    cmd = claude_cmd(query, model) if tool == "claude-code" else codex_cmd(query, model)
    parse = parse_claude_line if tool == "claude-code" else parse_codex_line
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, text=True)  # fmt: skip
    except OSError as e:
        run.error = str(e)
        return run
    timer = threading.Timer(timeout, p.kill)
    timer.start()
    try:
        assert p.stdout is not None
        for line in p.stdout:
            if parse(line, run):
                break
    finally:
        timer.cancel()
        p.kill()
        p.wait()
    run.seconds = round(time.monotonic() - start, 1)
    if not run.loaded and not run.text:
        run.error = run.error or "no decision in the output"
    return run


def triggered(case: Case, run: Run) -> bool:
    if case.skill in run.loaded:
        return True
    sign = EXPLICIT_SIGNS.get(case.skill)
    return bool(case.explicit and sign and sign.search(run.text))


def tool_query(tool: str, case: Case) -> str:
    if case.explicit and tool == "codex" and case.query.startswith("/"):
        return "$" + case.query[1:]
    return case.query


def score(cases: list[Case], runs: dict[int, list[Run]]) -> dict[str, Any]:
    by_skill: dict[str, dict[str, Any]] = {}
    for i, c in enumerate(cases):
        rs = runs.get(i, [])
        good = sum(1 for r in rs if r.error is None and triggered(c, r) == c.expect)
        s = by_skill.setdefault(c.skill, {"queries": 0, "passed": 0, "failures": []})
        s["queries"] += 1
        if good >= min(QUERY_PASS, len(rs)) and rs:
            s["passed"] += 1
        else:
            s["failures"].append({
                "query": c.query, "expect": "trigger" if c.expect else "no trigger",
                "correct_runs": good, "runs": len(rs),
                "loaded": [r.loaded for r in rs], "errors": [r.error for r in rs if r.error],
            })  # fmt: skip
    for s in by_skill.values():
        s["rate"] = round(s["passed"] / s["queries"], 3) if s["queries"] else 0.0
        s["pass"] = s["rate"] >= SKILL_PASS
    return by_skill


def report_md(tool: str, meta: dict[str, Any], skills: dict[str, Any]) -> str:
    lines = [f"# Trigger evals: {tool}", "",
             f"Run {meta['finished']} · {meta['runs_per_query']} runs per query · "
             f"model {meta['model'] or 'default'} · {meta['executed']} runs", "",
             "| Skill | Queries passed | Rate | Pass |", "|---|---|---|---|"]  # fmt: skip
    for name, s in sorted(skills.items()):
        lines.append(f"| {name} | {s['passed']}/{s['queries']} | {s['rate']:.0%} | "
                     f"{'yes' if s['pass'] else 'no'} |")  # fmt: skip
    if meta.get("stopped"):
        lines += ["", f"**Stopped early:** {meta['stopped']}"]
    for name, s in sorted(skills.items()):
        for f in s["failures"]:
            lines.append(
                f"- {name}: “{f['query']}” expected {f['expect']}, "
                f"{f['correct_runs']}/{f['runs']} runs right, loaded {f['loaded']}"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tool", choices=["claude-code", "codex"], required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--skills", nargs="*", default=None)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-utilization", type=float, default=0.8)
    args = ap.parse_args(argv)
    exe = "claude" if args.tool == "claude-code" else "codex"
    if not shutil.which(exe):
        print(f"{exe} is not installed; nothing was run.", file=sys.stderr)
        return 3
    cases = load_cases(only=set(args.skills) if args.skills else None)
    work = Path(tempfile.mkdtemp(prefix="jobhunt-trigger-"))
    sk.install(args.tool, sk.target_dir(args.tool, "project", work), mode="copy", force=True,
               dry_run=False)  # fmt: skip
    jobs = [(i, c) for i, c in enumerate(cases) for _ in range(args.runs)]
    runs: dict[int, list[Run]] = {}
    stopped: list[str] = []
    lock = threading.Lock()

    def one(item: tuple[int, Case]) -> None:
        i, c = item
        if stopped:
            return
        r = run_once(args.tool, tool_query(args.tool, c), work, args.model)
        with lock:
            runs.setdefault(i, []).append(r)
            if r.utilization is not None and r.utilization > args.max_utilization and not stopped:
                stopped.append(f"usage window at {r.utilization:.0%}")
            n = sum(len(v) for v in runs.values())
            mark = "ok" if r.error is None and triggered(c, r) == c.expect else "MISS"
            print(f"[{n}/{len(jobs)}] {mark} {c.skill}: {c.query[:60]} -> {r.loaded or '-'}",
                  file=sys.stderr, flush=True)  # fmt: skip

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        list(pool.map(one, jobs))
    shutil.rmtree(work, ignore_errors=True)
    skills = score(cases, runs)
    meta = {
        "tool": args.tool,
        "finished": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "runs_per_query": args.runs,
        "model": args.model,
        "executed": sum(len(v) for v in runs.values()),
        "stopped": stopped[0] if stopped else None,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    per_case = [
        {**asdict(c), "runs": [asdict(r) for r in runs.get(i, [])]} for i, c in enumerate(cases)
    ]
    detail = {"meta": meta, "skills": skills, "runs": per_case}
    (RESULTS / f"{args.tool}.json").write_text(json.dumps(detail, indent=1), encoding="utf-8")
    (RESULTS / f"{args.tool}.md").write_text(report_md(args.tool, meta, skills), encoding="utf-8")
    print(report_md(args.tool, meta, skills))
    return 0 if all(s["pass"] for s in skills.values()) and not stopped else 1


if __name__ == "__main__":
    raise SystemExit(main())
