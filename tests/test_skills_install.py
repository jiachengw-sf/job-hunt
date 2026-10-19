"""`jobhunt skills install`: both tools, explicit-only apply, never clobbering the user's own."""

from __future__ import annotations

from pathlib import Path

from jobhunt import skills as sk
from jobhunt.doctor import check_skills
from jobhunt.errors import ExitCode
from tests.conftest import REPO

SRC = REPO / "skills"


def _dirs() -> tuple[Path, Path]:
    return Path.home() / ".claude" / "skills", Path.home() / ".agents" / "skills"


def test_install_both_tools(cli):
    code, env, _ = cli("skills", "install", "--tool", "all")
    assert code == ExitCode.OK, env
    claude, codex = _dirs()
    for d in (claude, codex):
        assert sorted(p.name for p in d.iterdir() if p.is_dir()) == sk.skill_names()
    apply_c = (claude / "jobhunt-apply" / "SKILL.md").read_text()
    assert "disable-model-invocation: true" in apply_c.split("\n---\n")[0]
    assert not (claude / "jobhunt-apply" / "agents").exists()
    assert (codex / "jobhunt-apply" / "agents" / "openai.yaml").read_text() == sk.CODEX_SIDECAR
    assert (codex / "jobhunt-apply" / "SKILL.md").read_text() == (
        SRC / "jobhunt-apply" / "SKILL.md"
    ).read_text()
    assert (claude / "jobhunt-daily" / "SKILL.md").read_text() == (
        SRC / "jobhunt-daily" / "SKILL.md"
    ).read_text()
    assert (claude / "jobhunt-judge" / "references" / "result-format.md").is_file()
    # the shipped skills stay portable
    assert "disable-model-invocation" not in (SRC / "jobhunt-apply" / "SKILL.md").read_text()
    _, again, _ = cli("skills", "install", "--tool", "all")
    for o in again["data"]["tools"]:
        assert o["installed"] == [] and len(o["unchanged"]) == 7
    _, st, _ = cli("skills", "status", "--tool", "all")
    for o in st["data"]["tools"]:
        assert o["missing"] == [] and o["explicit_only_ok"] == {"jobhunt-apply": True}


def test_auto_detects_tools(cli):
    (Path.home() / ".codex").mkdir()
    code, env, _ = cli("skills", "install")
    assert code == ExitCode.OK
    assert [o["tool"] for o in env["data"]["tools"]][-1] == "codex"


def test_users_own_skill_is_never_overwritten(cli):
    claude, _ = _dirs()
    mine = claude / "jobhunt-daily"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: jobhunt-daily\ndescription: mine\n---\nmine\n")
    code, env, _ = cli("skills", "install", "--tool", "claude-code")
    assert code == ExitCode.OK
    assert "jobhunt-daily" in env["data"]["tools"][0]["skipped"]
    assert env["hints"]["user_actions"]
    assert "mine" in (mine / "SKILL.md").read_text()
    _, rm, _ = cli("skills", "remove", "--tool", "claude-code")
    assert "jobhunt-daily" not in rm["data"]["tools"][0]["removed"] and mine.exists()
    cli("skills", "install", "--tool", "claude-code", "--force")
    assert "mine" not in (mine / "SKILL.md").read_text()


def test_symlink_mode_copies_the_explicit_only_skill(cli):
    cli("skills", "install", "--tool", "codex", "--mode", "symlink")
    _, codex = _dirs()
    assert (codex / "jobhunt-daily").is_symlink()
    assert not (codex / "jobhunt-apply").is_symlink()
    assert sk.explicit_ok("codex", codex / "jobhunt-apply")


def test_project_scope_and_dry_run(cli, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    code, env, _ = cli("skills", "install", "--tool", "all", "--scope", "project",
                       "--project-dir", str(proj), "--dry-run")  # fmt: skip
    assert code == ExitCode.OK and len(env["data"]["tools"][0]["installed"]) == 7
    assert not (proj / ".claude").exists()
    cli("skills", "install", "--tool", "all", "--scope", "project", "--project-dir", str(proj))
    assert (proj / ".claude" / "skills" / "jobhunt-setup" / "SKILL.md").is_file()
    assert (proj / ".agents" / "skills" / "jobhunt-setup" / "SKILL.md").is_file()


def test_doctor_sees_installed_skills_and_a_loose_apply(cli):
    (Path.home() / ".claude").mkdir()
    cli("skills", "install", "--tool", "claude-code")
    [c] = [c for c in check_skills() if c.name == "skills.claude-code"]
    assert c.status == "ok"
    claude, _ = _dirs()
    md = claude / "jobhunt-apply" / "SKILL.md"
    md.write_text(md.read_text().replace("disable-model-invocation: true\n", ""))
    [c] = [c for c in check_skills() if c.name == "skills.claude-code"]
    assert c.status == "warn" and "without being asked" in c.summary
