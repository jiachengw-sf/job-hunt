"""Record the README demo: real jobhunt commands for a synthetic persona, offline.

    uv run python docs/demo/record.py            # writes docs/demo/session.json
    swift docs/demo/render.swift                 # macOS: session.json -> docs/demo.gif

Everything runs in a throwaway JOBHUNT_HOME with the synthetic `intl-sponsorship` persona and the
offline fixtures; no network, no real data. The judgment tasks your AI tool would do (the
jobhunt-judge skill) are done by the deterministic stand-in the tests use, and the screen says
so. Command output is shown as printed, cut where marked with "…", with the temp path shown as
~/.jobhunt.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tests.judge_helpers import judge_all  # noqa: E402

PERSONA = "intl-sponsorship"
OUT = Path(__file__).with_name("session.json")

# (kind, text, options). "say" prints a comment line, "run" types and runs a command; "persona"
# and "judge" happen off screen (onboarding files, the judgment stand-in) and the screen says so.
SCRIPT: list[tuple[str, str, dict]] = [
    ("run", "jobhunt doctor --fix --offline --human", {"skip_section": "Applied:"}),
    ("persona", "", {}),
    ("say", f"# Onboarding done for the synthetic persona '{PERSONA}'. Offline fixtures.", {}),
    ("run", "jobhunt run --human", {"head": 24}),
    ("say", "# Your AI tool does the judgment tasks (jobhunt-judge); a stand-in here.", {}),
    ("judge", "", {}),
    ("run", "jobhunt ingest > /dev/null", {}),
    ("run", "jobhunt match --human", {"head": 4}),
    ("run", "jobhunt digest --human", {"head": 27}),
    ("say", "# You applied and heard back. Record it (or edit Status in the sheet):", {}),
    ("run", "jobhunt mark 10 Applied > /dev/null", {}),
    ("run", "jobhunt mark 10 Interviewing > /dev/null", {}),
    ("run", "jobhunt mark 1 Applied > /dev/null", {}),
    ("run", "jobhunt mark 1 Rejected > /dev/null", {}),
    ("run", "jobhunt mark 5 Applied > /dev/null", {}),
    ("run", "jobhunt mark 3 Skipped --reason domain > /dev/null", {}),
    ("run", "jobhunt calibrate --human", {}),
]


def setup(home: Path, fixtures: Path) -> dict[str, str]:
    env = {**os.environ, "JOBHUNT_HOME": str(home), "JOBHUNT_FIXTURES": str(fixtures)}
    env["COLUMNS"] = "100"
    env["HOME"] = str(home.parent)  # doctor's skill checks look in a scratch home, not yours
    shutil.copytree(REPO / "tests" / "fixtures" / "sources" / "base", fixtures)
    shutil.copytree(REPO / "tests" / "fixtures" / "forms" / "overlay", fixtures, dirs_exist_ok=True)
    return env


def install_persona(home: Path) -> None:
    src = REPO / "tests" / "personas" / PERSONA
    for f in ("profile.yaml", "preferences.yaml", "answers.yaml", "watchlist.yaml"):
        shutil.copy(src / f, home / f)
    shutil.copy(src / "resume.md", home / "resume" / "resume.md")
    # Judge every job, not just the top priorities, so every band is final in the demo.
    (home / "config.yaml").write_text("fit:\n  priority_threshold: 0\n", encoding="utf-8")


def run(cmd: str, env: dict[str, str], home: Path) -> list[str]:
    argv = cmd.split(" > ")[0].split()
    p = subprocess.run(
        [sys.executable, "-m", "jobhunt", *argv[1:]],
        env=env,
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    if p.returncode not in (0, 9):
        raise SystemExit(f"{cmd} exited {p.returncode}:\n{p.stdout}\n{p.stderr}")
    if " > " in cmd:
        return []
    text = p.stdout
    for prefix in (str(home.resolve()), str(home)):
        text = text.replace(prefix, "~/.jobhunt")
    return text.rstrip("\n").splitlines()


def cut(lines: list[str], head: int | None, tail: int | None, skip: str | None) -> list[str]:
    if skip and skip in lines:  # drop a section: its header line up to the next blank line
        i = lines.index(skip)
        j = lines.index("", i) if "" in lines[i:] else len(lines)
        lines = [*lines[:i], "  …", *lines[j:]]
    if head is not None and len(lines) > head:
        return [*lines[:head], "  …"]
    if tail is not None and len(lines) > tail:
        return ["  …", *lines[-tail:]]
    return lines


def main() -> None:
    steps: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        home, fixtures = Path(tmp) / "home", Path(tmp) / "fixtures"
        env = setup(home, fixtures)
        for kind, text, opt in SCRIPT:
            if kind == "say":
                steps.append({"say": text})
            elif kind == "persona":
                install_persona(home)
            elif kind == "judge":
                judge_all(home)
            else:
                out = run(text, env, home)
                steps.append(
                    {
                        "cmd": text,
                        "out": cut(out, opt.get("head"), opt.get("tail"), opt.get("skip_section")),
                    }
                )
    OUT.write_text(json.dumps({"persona": PERSONA, "steps": steps}, indent=1) + "\n", "utf-8")
    print(f"wrote {OUT.relative_to(REPO)} ({len(steps)} steps)")


if __name__ == "__main__":
    main()
