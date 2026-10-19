from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jobhunt.config import (
    deep_merge,
    find_placeholders,
    load_config,
    load_user_file,
    onboarding_status,
    require_onboarding,
)
from jobhunt.errors import ConfigError, OnboardingIncomplete
from jobhunt.models import Answers, Preferences, Profile, Watchlist
from jobhunt.models.answers import DECLINE_EEO
from jobhunt.paths import get_paths
from tests.conftest import PERSONAS, PERSONAS_DIR, REPO


def test_defaults_load_without_user_config():
    cfg = load_config()
    assert cfg.prefill.batch_size == 5
    assert cfg.schedule.interval_hours == 4
    assert "jobhunt/" in cfg.http.user_agent and "{version}" not in cfg.http.user_agent


def test_user_config_overrides_only_given_keys(isolated_home: Path):
    isolated_home.mkdir(parents=True)
    (isolated_home / "config.yaml").write_text("prefill:\n  batch_size: 3\n", encoding="utf-8")
    cfg = load_config()
    assert cfg.prefill.batch_size == 3
    assert cfg.volume_guard.window_days == 90


def test_bad_yaml_reports_line(isolated_home: Path):
    isolated_home.mkdir(parents=True)
    (isolated_home / "config.yaml").write_text("prefill:\n  batch_size: [3\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="line"):
        load_config()


def test_http_interval_cannot_go_below_one_second(isolated_home: Path):
    isolated_home.mkdir(parents=True)
    (isolated_home / "config.yaml").write_text(
        "http:\n  min_host_interval_seconds: 0.1\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="min_host_interval_seconds"):
        load_config()


def test_research_budget_is_capped(isolated_home: Path):
    isolated_home.mkdir(parents=True)
    (isolated_home / "config.yaml").write_text("research:\n  max_searches: 50\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="max_searches"):
        load_config()


def test_deep_merge():
    assert deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}}) == {"a": {"b": 1, "c": 3}}


def test_find_placeholders():
    data = {"a": "CHANGE_ME", "b": ["ok", "<your name>"], "c": {"d": "fine"}}
    assert find_placeholders(data) == ["a", "b[1]"]


def test_jobhunt_home_override(isolated_home: Path):
    assert get_paths().home == isolated_home.resolve()


def test_onboarding_gate(isolated_home: Path, persona_home):
    with pytest.raises(OnboardingIncomplete):
        require_onboarding()
    persona_home("first-year-intern")
    require_onboarding()
    assert set(onboarding_status().values()) == {"ok"}


@pytest.mark.parametrize("persona", PERSONAS)
def test_personas_validate(persona):
    d = PERSONAS_DIR / persona
    Profile.model_validate(yaml.safe_load((d / "profile.yaml").read_text(encoding="utf-8")))
    Preferences.model_validate(yaml.safe_load((d / "preferences.yaml").read_text(encoding="utf-8")))
    Answers.model_validate(yaml.safe_load((d / "answers.yaml").read_text(encoding="utf-8")))
    assert (d / "resume.md").exists()


def test_personas_cover_required_scenarios():
    prefs = {
        p: Preferences.model_validate(
            yaml.safe_load((PERSONAS_DIR / p / "preferences.yaml").read_text(encoding="utf-8"))
        )
        for p in PERSONAS
    }
    answers = {
        p: Answers.model_validate(
            yaml.safe_load((PERSONAS_DIR / p / "answers.yaml").read_text(encoding="utf-8"))
        )
        for p in PERSONAS
    }
    assert prefs["first-year-intern"].roles.seniority.value == ["internship"]
    assert answers["intl-sponsorship"].work_authorization["US"].sponsorship_future == "yes"
    assert "full-time" in prefs["newgrad-analyst"].employment_types.value
    assert prefs["senior-backend"].location.work_modes.must
    assert answers["senior-backend"].experience.years >= 8


def test_examples_validate_and_carry_placeholders():
    for name, model in [
        ("profile", Profile),
        ("preferences", Preferences),
        ("answers", Answers),
        ("watchlist", Watchlist),
    ]:
        data = yaml.safe_load((REPO / f"{name}.example.yaml").read_text(encoding="utf-8"))
        model.model_validate(data)
    # copying an example unedited must not pass onboarding
    assert find_placeholders(
        yaml.safe_load((REPO / "profile.example.yaml").read_text(encoding="utf-8"))
    )
    assert find_placeholders(
        yaml.safe_load((REPO / "answers.example.yaml").read_text(encoding="utf-8"))
    )


def test_unedited_example_fails_onboarding(isolated_home: Path):
    isolated_home.mkdir(parents=True)
    (isolated_home / "profile.yaml").write_text(
        (REPO / "profile.example.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="placeholder"):
        load_user_file("profile")


def test_pref_shorthand_is_nice_and_explicit_must():
    p = Preferences.model_validate(
        {
            "roles": {
                "families": ["data"],
                "seniority": {"value": ["entry"], "importance": "must"},
            },
            "pay": {"min": 50000, "unit": "annual"},
        }
    )
    assert p.roles.families.importance == "nice"
    assert p.roles.seniority.must
    assert p.pay.value.min == 50000 and not p.pay.must


def test_unknown_keys_rejected():
    with pytest.raises(ValueError):
        Preferences.model_validate({"rolez": {}})
    with pytest.raises(ValueError):
        Answers.model_validate({"identity": {"ssn": "x"}})


def test_eeo_defaults_to_decline():
    a = Answers.model_validate({})
    eeo = a.eeo.model_dump()
    assert eeo.pop("confirmed") is False
    assert all(v == DECLINE_EEO for v in eeo.values())


def test_sensitive_fields_default_to_unset_not_guessed():
    a = Answers.model_validate({})
    assert a.work_authorization == {}
    assert a.identity.over_18 is None
    assert a.standard.background_check_consent is None
    assert a.compensation.expected is None
