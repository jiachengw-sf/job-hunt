from __future__ import annotations

from pathlib import Path

import pytest

from jobhunt.errors import ExitCode
from tests.conftest import PERSONAS


def _checks(env: dict) -> dict[str, dict]:
    return {c["name"]: c for c in env["data"]["checks"]}


def test_fresh_install_fails_with_fixes(cli, isolated_home: Path):
    code, env, _ = cli("doctor", "--offline")
    assert code == ExitCode.CHECKS_FAILED
    assert env["ok"] is False and env["error"]["code"] == "checks_failed"
    checks = _checks(env)
    assert checks["data_dir"]["status"] == "fail"
    assert checks["store"]["status"] == "fail"
    # every non-ok check names its fix
    for c in env["data"]["checks"]:
        if c["status"] in ("warn", "fail"):
            assert c["fix"], f"{c['name']} has no fix"
    assert "Run `jobhunt doctor --fix`." in env["hints"]["next_actions"]
    # a read-only doctor run creates nothing
    assert not isolated_home.exists()


def test_fix_dry_run_writes_nothing(cli, isolated_home: Path):
    code, env, _ = cli("doctor", "--offline", "--fix", "--dry-run")
    assert env["data"]["dry_run"] is True
    assert any("create" in f for f in env["data"]["fixed"])
    assert not isolated_home.exists()
    assert code == ExitCode.CHECKS_FAILED


def test_fix_then_rerun_is_idempotent(cli, isolated_home: Path):
    code, env, _ = cli("doctor", "--offline", "--fix")
    assert code == ExitCode.OK, env
    assert (isolated_home / "jobhunt.db").exists()
    assert (isolated_home / "config.yaml").exists()
    code2, env2, _ = cli("doctor", "--offline", "--fix")
    assert code2 == ExitCode.OK
    assert env2["data"]["fixed"] == []


def test_offline_skips_sources(cli):
    _, env, _ = cli("doctor", "--offline")
    assert _checks(env)["sources"]["status"] == "skip"


def test_invalid_config_fails_with_location(cli, isolated_home: Path):
    cli("doctor", "--offline", "--fix")
    (isolated_home / "config.yaml").write_text(
        "version: 1\nprefill:\n  batch_sise: 3\n", encoding="utf-8"
    )
    code, env, _ = cli("doctor", "--offline")
    assert code == ExitCode.CHECKS_FAILED
    cfg = _checks(env)["config"]
    assert cfg["status"] == "fail"
    assert "prefill.batch_sise" in cfg["summary"]


def test_placeholder_config_fails(cli, isolated_home: Path):
    cli("doctor", "--offline", "--fix")
    (isolated_home / "config.yaml").write_text(
        "version: 1\nnotifications:\n  ntfy_topic: CHANGE_ME\n", encoding="utf-8"
    )
    _, env, _ = cli("doctor", "--offline")
    cfg = _checks(env)["config"]
    assert cfg["status"] == "fail" and "notifications.ntfy_topic" in cfg["summary"]


def test_onboarding_missing_is_warning_with_setup_hint(cli):
    cli("doctor", "--offline", "--fix")
    code, env, _ = cli("doctor", "--offline")
    assert code == ExitCode.OK
    assert _checks(env)["onboarding.profile"]["status"] == "warn"
    assert any("jobhunt-setup" in a for a in env["hints"]["next_actions"])


@pytest.mark.parametrize("persona", PERSONAS)
def test_persona_onboarding_files_pass(cli, persona_home, persona):
    persona_home(persona)
    code, env, _ = cli("doctor", "--offline", "--fix")
    assert code == ExitCode.OK, env
    checks = _checks(env)
    for kind in ("profile", "preferences", "answers"):
        assert checks[f"onboarding.{kind}"]["status"] == "ok", checks[f"onboarding.{kind}"]


def test_broken_persona_file_fails_with_reason(cli, persona_home):
    home = persona_home("senior-backend")
    (home / "preferences.yaml").write_text(
        "version: 1\nroles:\n  seniority:\n    value: [wizard]\n", encoding="utf-8"
    )
    code, env, _ = cli("doctor", "--offline", "--fix")
    assert code == ExitCode.CHECKS_FAILED
    c = _checks(env)["onboarding.preferences"]
    assert c["status"] == "fail" and "roles.seniority" in c["summary"]


def test_skills_check_detects_claude_dir(cli, isolated_home: Path):
    (isolated_home.parent / ".claude").mkdir()
    _, env, _ = cli("doctor", "--offline")
    c = _checks(env)["skills.claude-code"]
    assert c["status"] == "warn" and "0/8" in c["summary"]


def test_human_output(capsys):
    from jobhunt.cli.main import run

    run(["doctor", "--offline", "--human"])
    out = capsys.readouterr().out
    assert "[FAIL] data_dir" in out and "fix: Run `jobhunt doctor --fix`." in out
