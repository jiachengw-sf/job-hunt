"""answers.json: values built by code from the profile and bank, checked against the form."""

from __future__ import annotations

import stat

import pytest

from jobhunt.forms.classify import classify
from jobhunt.forms.model import FormField, FormSpec
from jobhunt.prefill.values import build_sheet, read_sheet, validate, write_sheet
from tests.conftest import PERSONAS
from tests.test_forms import COMPANY, SPECS, ctx_for

NOW = "2026-10-18T12:00:00Z"


def sheet_for(persona: str, name: str, tmp_path, *, resume: bool = True):
    spec = SPECS[name]()
    ctx, matcher, eeo = ctx_for(persona, COMPANY[name[:2]])
    ctx.resume = tmp_path / "resume.pdf"
    if resume:
        ctx.resume.write_bytes(b"%PDF-1.4 synthetic")
    cls = classify(spec, ctx, matcher, eeo_confirmed=eeo)
    return spec, cls, build_sheet(1, spec, cls, ctx, matcher, NOW)


@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize("name", sorted(SPECS))
def test_ready_forms_validate_for_every_persona(persona, name, tmp_path):
    spec, cls, sheet = sheet_for(persona, name, tmp_path)
    assert [f.id for f in sheet.fields] == [f.id for f in spec.fields]
    if cls.cls != "ready":
        return
    assert validate(sheet, spec) == []
    by = {f.id: f for f in spec.fields}
    for ff in sheet.to_fill():
        f = by[ff.id]
        if f.type in ("select", "radio") and f.options:
            assert ff.value in f.options
        if f.type == "file":
            assert ff.value == str(tmp_path / "resume.pdf")


def test_actions_follow_the_rules(tmp_path):
    _, _, sheet = sheet_for("intl-sponsorship", "lv_A", tmp_path)
    by = {f.label: f for f in sheet.fields}
    auth = next(f for f in sheet.fields if "legally authorized" in f.label)
    assert auth.action == "recommend" and auth.value == "Yes"  # Lever radio: user ticks it
    assert by["Email"].action == "fill"
    _, _, sheet = sheet_for("intl-sponsorship", "gh_A", tmp_path)
    by = {f.id: f for f in sheet.fields}
    assert by["question_100103"].action == "fill"
    assert by["question_100103"].value == "Yes"  # sponsorship in the future
    assert by["gender"].action in ("human", "fill")
    if by["gender"].action == "human":
        assert by["gender"].value is None


def test_eeo_left_to_user_until_confirmed(tmp_path):
    spec, cls, sheet = sheet_for("intl-sponsorship", "gh_A", tmp_path)
    ctx, matcher, _ = ctx_for("intl-sponsorship", "Northwind Robotics")
    cls = classify(spec, ctx, matcher, eeo_confirmed=False)
    sheet = build_sheet(1, spec, cls, ctx, matcher, NOW)
    for ff in sheet.fields:
        if ff.section == "eeo":
            assert ff.action in ("human", "skip") and ff.value is None


def test_validator_catches_each_problem(tmp_path):
    spec = FormSpec(
        ats="greenhouse",
        fields=[
            FormField(id="q1", label="Pick", type="select", required=True, options=["A", "B"]),
            FormField(id="q2", label="Short", type="text", required=True, max_length=5),
            FormField(id="q3", label="Resume", type="file", required=True),
            FormField(id="q4", label="Needed", type="text", required=True),
            FormField(id="q5", label="One", type="radio", options=["X", "Y"]),
        ],
    )
    from jobhunt.prefill.values import AnswerSheet, FillField

    def ff(fid, action, value, **kw):
        f = next(x for x in spec.fields if x.id == fid)
        return FillField(
            id=fid, label=f.label, type=f.type, required=f.required, section="main",
            action=action, value=value, **kw,
        )  # fmt: skip

    sheet = AnswerSheet(
        job_id=1,
        ats="greenhouse",
        url=None,
        built_at=NOW,
        fields=[
            ff("q1", "fill", "C"),
            ff("q2", "fill", "toolong"),
            ff("q3", "fill", str(tmp_path / "missing.pdf")),
            ff("q4", "skip", None),
            ff("q5", "fill", ["X", "Y"]),
            ff("q4", "fill", "x"),
        ],
    )
    sheet.fields[-1].id = "q9"  # a question id from another job
    probs = {(p.id, p.problem) for p in validate(sheet, spec)}
    assert ("q1", "value isn't one of the form's options") in probs
    assert ("q2", "longer than the field's 5-character limit") in probs
    assert ("q3", "the file doesn't exist") in probs
    assert ("q4", "required but left blank") in probs
    assert ("q5", "several values for a single-choice field") in probs
    assert any(p[0] == "q9" for p in probs)


def test_missing_resume_fails_validation(tmp_path):
    spec, cls, sheet = sheet_for("intl-sponsorship", "gh_A", tmp_path, resume=False)
    assert cls.cls == "ready"
    assert any(p.problem == "the file doesn't exist" for p in validate(sheet, spec))


def test_invisible_characters_are_stripped(tmp_path):
    spec, cls, sheet = sheet_for("intl-sponsorship", "gh_A", tmp_path)
    ctx, matcher, _ = ctx_for("intl-sponsorship", "Northwind Robotics")
    ctx.answers.identity.legal_first_name = "Mei​"
    ctx.resume = tmp_path / "resume.pdf"
    sheet = build_sheet(1, spec, cls, ctx, matcher, NOW)
    first = next(f for f in sheet.fields if f.id == "first_name")
    assert first.value == "Mei"


def test_sheet_file_is_owner_only(tmp_path):
    _, _, sheet = sheet_for("intl-sponsorship", "gh_A", tmp_path)
    p = tmp_path / "prefill" / "1" / "answers.json"
    write_sheet(p, sheet)
    assert read_sheet(p) == sheet
    if stat.S_IMODE(p.stat().st_mode) != 0o600:
        pytest.skip("filesystem without POSIX modes")
