"""End-to-end check of the daily flow, driven by a real AI tool through the installed skills.

Sets up a synthetic persona in a scratch ``JOBHUNT_HOME`` with offline fixtures
(``JOBHUNT_FIXTURES``), installs the skills for the tool in a scratch project, and asks the tool
"Run my job hunt." headless. The tool may run only ``jobhunt`` commands and read/write files.
Afterwards the store is checked:

- the pipeline ran, judgment tasks were done and ingested, bands stopped being provisional
- the digest was shown and the batched questions were reached
- nothing was pre-filled, submitted or sent

    uv run python -m jobhunt.devtools.daily_e2e --tool claude-code [--persona intl-sponsorship]

Results: ``evals/daily/results/<tool>.md`` and ``.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jobhunt import skills as sk

REPO = Path(__file__).resolve().parents[3]
RESULTS = REPO / "evals" / "daily" / "results"
PROMPT = "Run my job hunt."


def setup_home(root: Path, persona: str) -> tuple[Path, Path]:
    home = root / "jobhunt-home"
    (home / "resume").mkdir(parents=True)
    src = REPO / "tests" / "personas" / persona
    for f in ("profile.yaml", "preferences.yaml", "answers.yaml", "watchlist.yaml"):
        if (src / f).exists():
            shutil.copy(src / f, home / f)
    shutil.copy(src / "resume.md", home / "resume" / "resume.md")
    # small enough to finish in one session: a judgment task for every recalled job
    (home / "config.yaml").write_text("fit:\n  priority_threshold: 0\n", encoding="utf-8")
    fx = root / "fixtures"
    shutil.copytree(REPO / "tests" / "fixtures" / "sources" / "base", fx)
    shutil.copytree(REPO / "tests" / "fixtures" / "forms" / "overlay", fx, dirs_exist_ok=True)
    return home, fx


def claude_cmd(home: Path, budget: float) -> list[str]:
    return [
        "claude", "-p", PROMPT, "--output-format", "stream-json", "--verbose",
        "--allowedTools", "Bash(jobhunt *)", "Bash(jobhunt:*)", "Read", "Write", "Edit",
        "Glob", "Grep", "Skill",
        "--add-dir", str(home), "--strict-mcp-config", "--no-session-persistence",
        "--max-budget-usd", str(budget),
    ]  # fmt: skip


def codex_cmd(home: Path, budget: float) -> list[str]:
    return ["codex", "exec", "--json", "--skip-git-repo-check", "--full-auto", PROMPT]


def transcript(tool: str, lines: list[str]) -> dict[str, Any]:
    commands, final, cost = [], "", None
    for ln in lines:
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if tool == "claude-code":
            if d.get("type") == "assistant":
                for c in d["message"].get("content", []):
                    if c.get("type") == "tool_use" and c.get("name") == "Bash":
                        commands.append(c["input"].get("command", ""))
                    elif c.get("type") == "tool_use" and c.get("name") == "Skill":
                        commands.append(f"[skill] {c['input'].get('skill')}")
                    elif c.get("type") == "text":
                        final = c["text"]
            elif d.get("type") == "result":
                cost = d.get("total_cost_usd")
        else:
            item = d.get("item") or {}
            if item.get("type") == "command_execution":
                commands.append(str(item.get("command", "")))
            elif item.get("type") in ("agent_message", "assistant_message"):
                final = str(item.get("text", ""))
    return {"commands": commands, "final": final, "cost_usd": cost}


def checks(home: Path, t: dict[str, Any]) -> dict[str, bool]:
    db = sqlite3.connect(home / "jobhunt.db")
    db.row_factory = sqlite3.Row
    q = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731
    cmds = " \n".join(t["commands"])
    out = {
        "used the daily skill": "[skill] jobhunt-daily" in cmds,
        "pipeline ran": q("SELECT count(*) FROM runs WHERE command='match'") >= 1,
        "judgment tasks ingested": q(
            "SELECT count(*) FROM tasks WHERE kind='requirement_evidence' AND status='ingested'"
        ) >= 1,
        "some bands no longer provisional": q(
            "SELECT count(*) FROM fits WHERE band IS NOT NULL AND provisional=0"
        ) >= 1,
        "digest shown": "jobhunt digest" in cmds,
        "batched questions reached": "jobhunt answers questions" in cmds,
        "no pre-fill started": "jobhunt prefill" not in cmds.replace("prefill --dry-run", "")
        and not (home / "work" / "prefill" / "state.json").exists(),
        "nothing marked applied": q(
            "SELECT count(*) FROM applications WHERE status NOT IN ('New')"
        ) == 0,
        "nothing sent": q("SELECT count(*) FROM drafts WHERE status='sent'") == 0
        and q("SELECT count(*) FROM notifications WHERE channel != 'terminal'") == 0,
    }  # fmt: skip
    db.close()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tool", choices=["claude-code", "codex"], required=True)
    ap.add_argument("--persona", default="intl-sponsorship")
    ap.add_argument("--budget", type=float, default=5.0, help="Claude Code --max-budget-usd.")
    ap.add_argument("--timeout", type=float, default=1800)
    args = ap.parse_args(argv)
    exe = "claude" if args.tool == "claude-code" else "codex"
    if not shutil.which(exe):
        print(f"{exe} is not installed; nothing was run.", file=sys.stderr)
        return 3
    root = Path(tempfile.mkdtemp(prefix="jobhunt-daily-"))
    home, fx = setup_home(root, args.persona)
    project = root / "project"
    project.mkdir()
    sk.install(args.tool, sk.target_dir(args.tool, "project", project), mode="copy", force=True,
               dry_run=False)  # fmt: skip
    jobhunt_bin = shutil.which("jobhunt") or str(Path(sys.executable).parent / "jobhunt")
    path = f"{Path(jobhunt_bin).parent}{os.pathsep}{os.environ.get('PATH', '')}"
    env = {**os.environ, "JOBHUNT_HOME": str(home), "JOBHUNT_FIXTURES": str(fx), "PATH": path}
    cmd = (claude_cmd if args.tool == "claude-code" else codex_cmd)(home, args.budget)
    start = time.monotonic()
    p = subprocess.run(cmd, cwd=project, env=env, capture_output=True, text=True,
                       timeout=args.timeout, stdin=subprocess.DEVNULL)  # fmt: skip
    t = transcript(args.tool, p.stdout.splitlines())
    c = checks(home, t)
    result = {
        "tool": args.tool, "persona": args.persona,
        "finished": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "seconds": round(time.monotonic() - start), "exit": p.returncode, "checks": c, **t,
    }  # fmt: skip
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{args.tool}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    head = (
        f"{result['finished']} · persona `{args.persona}` (synthetic, offline fixtures) · "
        f"{result['seconds']}s · cost {t['cost_usd']}"
    )
    md = [f"# Daily flow: {args.tool}", "", head, "", "| Check | Result |", "|---|---|"]
    md += [f"| {k} | {'pass' if v else 'FAIL'} |" for k, v in c.items()]
    md += ["", "Commands run, in order:", ""] + [f"- `{x}`" for x in t["commands"]]
    md += ["", "Final message:", "", *[f"> {ln}" for ln in t["final"].splitlines()]]
    (RESULTS / f"{args.tool}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    shutil.rmtree(root, ignore_errors=True)
    return 0 if all(c.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
