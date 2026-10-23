"""Answer-bank commands: sensitive answers stay out of chat and out of output."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from jobhunt.errors import ExitCode
from jobhunt.models import Answers
from tests.conftest import PERSONAS_DIR


def secrets_of(persona: str) -> list[str]:
    a = yaml.safe_load((PERSONAS_DIR / persona / "answers.yaml").read_text(encoding="utf-8"))
    ident = a.get("identity") or {}
    out = [ident.get(k) for k in ("email", "phone", "legal_last_name", "address_line1")]
    return [s for s in out if s]


def test_answers_add_refuses_sensitive_and_show_hides_values(cli, persona_home, tmp_path):
    persona_home("intl-sponsorship")
    code, env, _ = cli(
        "answers", "add", "--question", "Will you require visa sponsorship?", "--answer", "yes"
    )
    assert code == ExitCode.NEEDS_USER
    assert "form" in env["hints"]["user_actions"][0]
    code, env, _ = cli("answers", "show")  # large: spilled to a file
    assert code == ExitCode.OK
    env = {**env, "data": json.loads(Path(env["page"]["out_file"]).read_text(encoding="utf-8"))}
    rows = {r["key"]: r for r in env["data"]["builtin"]}
    assert rows["identity.email"]["answered"] and rows["identity.email"]["value"] is None
    assert rows["work_auth.sponsorship"]["value"] is None
    assert rows["sourcing.how_did_you_hear"]["value"] == "Job board"
    for s in secrets_of("intl-sponsorship"):
        assert s not in json.dumps(env)


def test_answers_add_edit_and_confirm(cli, persona_home):
    persona_home("newgrad-analyst")
    q = "Do you have a valid driver's license?"
    code, env, _ = cli("answers", "add", "--question", q, "--answer", "Yes", "--option", "Yes")
    assert code == ExitCode.OK, env
    key = env["data"]["key"]
    assert key.startswith("custom.") and env["data"]["entry"]["value"] == "Yes"
    code, _, _ = cli("answers", "add", "--question", q, "--answer", "No")
    assert code == ExitCode.VALIDATION  # exists: edit it instead
    code, env, _ = cli(
        "answers", "edit", key, "--answer", "No", "--add-wording", "Driver's license?"
    )
    assert code == ExitCode.OK and env["data"]["entry"]["value"] == "No"
    assert "Driver's license?" in env["data"]["entry"]["wordings"]
    code, env, _ = cli("answers", "confirm", "Are you at least eighteen?", "identity.over_18")
    assert code == ExitCode.OK
    assert env["data"]["entry"]["wordings"] == ["Are you at least eighteen?"]
    code, env, _ = cli("answers", "edit", "identity.over_18", "--answer", "yes")
    assert code == ExitCode.NEEDS_USER  # sensitive: only the local form sets it
    assert "setup form" in env["hints"]["user_actions"][0]
    code, _, _ = cli("answers", "confirm", "Anything", "no.such.key")
    assert code == ExitCode.NOT_FOUND


def test_non_sensitive_builtins_can_be_edited_from_the_cli(cli, persona_home):
    home = persona_home("newgrad-analyst")
    for key, answer in [
        ("education.graduation", "2027-12"),
        ("location.relocate", "no"),
        ("location.relocate_to", "us, ca"),
        ("location.onsite", "yes"),
        ("availability.start_date", "2027-06-01"),
        ("education.further_study", "undecided"),
        ("standard.other_offers", "no"),
    ]:
        code, env, _ = cli("answers", "edit", key, "--answer", answer)
        assert code == ExitCode.OK, (key, env)
    a = Answers.model_validate(yaml.safe_load((home / "answers.yaml").read_text(encoding="utf-8")))
    assert (a.education.graduation_year, a.education.graduation_month) == (2027, 12)
    assert a.location.willing_to_relocate == "no"
    assert a.location.relocate_anywhere_in == ["US", "CA"]
    assert a.location.onsite_full_time == "yes" and str(a.availability.start_date) == "2027-06-01"
    assert a.education.further_study == "undecided" and a.standard.other_offers == "no"
    assert not [e for e in a.bank if e.key == "location.relocate"]  # no empty entry left behind
    code, _, _ = cli("answers", "edit", "location.relocate", "--answer", "maybe")
    assert code == ExitCode.VALIDATION
    code, env, _ = cli("answers", "edit", "education.school", "--answer", "Elsewhere U")
    assert code == ExitCode.VALIDATION and "profile set" in env["hints"]["next_actions"][0]
    for sensitive in ("education.gpa", "education.sat_score", "identity.school_email"):
        code, _, _ = cli("answers", "edit", sensitive, "--answer", "4.0")
        assert code == ExitCode.NEEDS_USER, sensitive


def test_confirm_takes_several_labels(cli, persona_home):
    persona_home("intl-sponsorship")
    labels = [
        "Would you require visa sponsorship to work here in the future?",
        "Will you need us to sponsor a visa for you at some point in the future?",
    ]
    code, env, _ = cli("answers", "confirm", *labels, "work_auth.sponsorship_future")
    assert code == ExitCode.OK, env
    assert env["data"]["entry"]["wordings"] == labels
    code, _, _ = cli("answers", "confirm", "work_auth.sponsorship_future")
    assert code == ExitCode.VALIDATION  # a label is needed
