"""Specific wordings: a question that looks like one the user answered but asks the opposite, a
different number, or a different thing is never filled from that answer. Age questions are
answered from the user's age. Checked for every persona, with answer reuse on (the default)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from jobhunt.bank.keys import AGE_CHECK, BUILTIN, age_question
from jobhunt.bank.learn import seen_key
from jobhunt.bank.match import canonical, normalize, same_question
from jobhunt.forms.classify import classify, fill_value
from jobhunt.forms.model import FormField, FormSpec
from jobhunt.models import Answers
from jobhunt.onboarding.form import _stamp_age
from jobhunt.prefill.driver import observe_similar
from jobhunt.prefill.values import AnswerSheet, FillField, write_sheet
from tests.test_forms import ctx_for

PERSONAS = ["first-year-intern", "newgrad-analyst", "intl-sponsorship", "senior-backend"]


def yesno(label: str) -> FormSpec:
    f = FormField(id="q", label=label, type="select", required=True, options=["Yes", "No"])
    return FormSpec(ats="greenhouse", url="https://example.test/apply", fields=[f])


def same(a: str, b: str) -> bool:
    return same_question(canonical(normalize(a)), canonical(normalize(b)))


# (looks like ..., the question the user answered): never the same question
DIFFERENT = [
    ("Are you NOT legally authorized to work in the United States?",
     "Are you legally authorized to work in the United States?"),
    ("Are you unable to legally work in the United States?",
     "Are you able to legally work in the United States?"),
    ("Are you ineligible to work in the US?", "Are you eligible to work in the US?"),
    ("Can you work in the US without visa sponsorship?",
     "Will you require visa sponsorship to work in the US?"),
    ("Do you NOT require visa sponsorship?", "Do you require visa sponsorship?"),
    ("Won't you need sponsorship in the future?", "Will you need sponsorship in the future?"),
    ("Are you no longer a student?", "Are you a student?"),
    ("Are you under 18 years of age?", "Are you 18 years of age or older?"),
    ("Are you at least 21 years old?", "Are you at least 18 years old?"),
    ("Do you have at least 3 years of experience?", "Do you have experience with Python?"),
    ("Are you unwilling to relocate?", "Are you willing to relocate?"),
    ("Have you previously applied to Acme?", "Have you previously worked at Acme?"),
    ("Do you have relatives who previously worked at Acme?", "Have you previously worked at Acme?"),
]  # fmt: skip

# rewordings of the same question
SAME = [
    ("Are you lawfully permitted to work in the US?",
     "Are you legally authorized to work in the United States?"),
    ("Is Acme able to rely on you being available all summer?",
     "Are you available for the full 10-12 week summer internship?"),
    ("Would you be willing to relocate for this role?", "Are you willing to relocate?"),
    ("Do you not require sponsorship, now or later?", "Do you not need sponsorship now or later?"),
]  # fmt: skip


@pytest.mark.parametrize(("label", "known"), DIFFERENT)
def test_different_questions_are_never_the_same(label, known):
    assert not same(label, known)


@pytest.mark.parametrize(("label", "known"), SAME)
def test_rewordings_are_the_same(label, known):
    assert same(label, known)


# Each would get the opposite (or an invented) answer if it reused the look-alike's answer.
TRAPS = [
    "Are you NOT legally authorized to work in the United States?",
    "Are you unable to legally work in the United States?",
    "Do you NOT require visa sponsorship to work in the US?",
    "Will you not require sponsorship now or in the future?",
    "Can you work in the US without visa sponsorship?",
    "Are you unwilling to relocate?",
    "Have you previously applied to Acme?",
    "Are you 18?",  # no direction: not an age check
]


@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize("label", TRAPS)
def test_a_look_alike_question_is_never_filled(persona, label):
    ctx, matcher, eeo = ctx_for(persona, "Acme")
    matcher.fill_similar = True
    fr = classify(yesno(label), ctx, matcher, eeo_confirmed=eeo).fields[0]
    assert fr.resolution != "answered", (persona, label, fr.key, fr.match)


@pytest.mark.parametrize("persona", PERSONAS)
def test_sensitive_questions_fill_only_from_confirmed_wordings(persona):
    ctx, matcher, eeo = ctx_for(persona, "Acme")
    matcher.fill_similar = True
    for label in ["Will you, now or in the future, need Acme to sponsor an employment visa?",
                  "Do you hold an active security clearance?"]:  # fmt: skip
        fr = classify(yesno(label), ctx, matcher, eeo_confirmed=eeo).fields[0]
        assert fr.match != "candidate" or fr.resolution != "answered", (persona, label)


# --- age ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "want"),
    [
        ("Are you at least 21 years old?", ("above", 21)),
        ("Are you 18 years of age or older?", ("above", 18)),
        ("Are you under the age of 18?", ("below", 18)),
        ("Are you over the age of 16?", ("above", 16)),
        ("Are you 21+?", ("above", 21)),
        ("Are you NOT at least 18?", None),
        ("Are you 18?", None),
        ("Are you between 18 and 25 years old?", None),
        ("How old are you?", None),
    ],
)
def test_age_questions(label, want):
    assert age_question(label) == want


def _age_ctx(persona: str, age: int | None, as_of: str | None, today: date):
    ctx, matcher, eeo = ctx_for(persona, "Acme")
    ident = ctx.answers.identity.model_copy(
        update={
            "age": age,
            "age_as_of": date.fromisoformat(as_of) if as_of else None,
            "over_18": None,
        }
    )
    ctx.answers = ctx.answers.model_copy(update={"identity": ident})
    return replace(ctx, today=today), matcher, eeo


@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize(
    ("today", "label", "want"),
    [
        (date(2026, 11, 7), "Are you at least 21 years old?", "No"),
        (date(2026, 11, 7), "Are you under 18 years of age?", "No"),
        (date(2026, 11, 7), "Are you 18 years of age or older?", "Yes"),
        (date(2027, 6, 1), "Are you at least 20 years old?", None),  # birthday may have passed
        (date(2028, 12, 1), "Are you at least 21 years old?", "Yes"),  # counted up on its own
    ],
)
def test_age_questions_are_answered_from_the_age(persona, today, label, want):
    ctx, matcher, eeo = _age_ctx(persona, 19, "2026-11-07", today)
    matcher.fill_similar = True
    spec = yesno(label)
    fr = classify(spec, ctx, matcher, eeo_confirmed=eeo).fields[0]
    assert fr.key == AGE_CHECK and fr.sensitive
    if want is None:
        assert fr.resolution == "unanswered"
    else:
        assert fr.resolution == "answered"
        assert fill_value(fr, ctx, matcher, spec.fields[0].options) == want


def test_without_an_age_the_18_answer_still_works_and_others_are_asked():
    ctx, matcher, eeo = ctx_for("first-year-intern", "Acme")  # over_18: yes, no age
    under = classify(yesno("Are you under 18 years of age?"), ctx, matcher, eeo_confirmed=eeo)
    assert fill_value(under.fields[0], ctx, matcher, ["Yes", "No"]) == "No"
    at21 = classify(yesno("Are you at least 21 years old?"), ctx, matcher, eeo_confirmed=eeo)
    assert at21.fields[0].resolution == "unanswered"


def test_the_form_dates_a_new_age():
    ans = {"identity": {"age": 19}}
    _stamp_age(ans, {})
    assert ans["identity"]["age_as_of"] == date.today().isoformat()
    kept = {"identity": {"age": 19, "age_as_of": "2025-01-01"}}
    _stamp_age(kept, {"identity": {"age": 19}})
    assert kept["identity"]["age_as_of"] == "2025-01-01"  # unchanged age keeps its date
    gone = {"identity": {"age_as_of": "2025-01-01"}}
    _stamp_age(gone, {"identity": {"age": 19}})
    assert "age_as_of" not in gone["identity"]
    Answers.model_validate({"identity": {"age": 19, "age_as_of": "2026-11-07"}})


# --- previously worked here --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("employers", "company", "want"),
    [
        (["Amazon"], "Amazon", "yes"),
        (["Amazon Inc."], "Amazon", "yes"),
        (["Amazon Web Services"], "Amazon", None),  # related: only the user knows
        (["Meta"], "Meta Platforms", None),
        (["Microsoft Research"], "Microsoft", None),
        (["Northfield State University"], "Lakeshore Logistics", "no"),
        (["Global Systems Group"], "Global Payments", "no"),  # only generic words shared
    ],
)
def test_previously_employed(employers, company, want):
    ctx, _, _ = ctx_for("first-year-intern", company)
    ctx.answers = ctx.answers.model_copy(
        update={"experience": ctx.answers.experience.model_copy(
            update={"prior_employers": employers})}
    )  # fmt: skip
    ctx.profile = ctx.profile.model_copy(update={"experience": []})
    assert BUILTIN["experience.previously_employed"].resolve(ctx) == want


# --- learning only what the user kept -----------------------------------------------------------


class _Page:
    def is_closed(self) -> bool:
        return False


class _Adapter:
    def __init__(self, values: dict[str, str | None]) -> None:
        self.values = values

    def _safe_read(self, page, ff):
        return self.values.get(ff.id)

    def matches(self, ff, actual):
        return actual == ff.value


def test_observe_records_whether_the_user_kept_each_similar_fill(tmp_path: Path):
    from jobhunt.prefill.driver import Tab

    def ff(fid, label, key, similar=True):
        return FillField(id=fid, label=label, type="text", required=True, section="main",
                         key=key, action="fill", value="Yes", similar=similar)  # fmt: skip

    sheet = AnswerSheet(job_id=1, ats="greenhouse", url=None, built_at="t", fields=[
        ff("a", "All summer?", "start.summer_availability"),
        ff("b", "Move for us?", "location.relocate"),
        ff("c", "Not read", "availability.start_date"),
        ff("d", "First name", "identity.first_name", similar=False),
    ])  # fmt: skip
    p = tmp_path / "answers.json"
    write_sheet(p, sheet)
    job = type("J", (), {"sheet": str(p), "job_id": 1})()
    tab = Tab(job=job, adapter=_Adapter({"a": "Yes", "b": "No", "d": "Riley"}), page=_Page())  # type: ignore[arg-type]
    observe_similar(tab)
    assert tab.extra["similar_kept"] == {
        seen_key("All summer?", "start.summer_availability"): True,
        seen_key("Move for us?", "location.relocate"): False,
    }
    tab.adapter.values["b"] = "Yes"  # the user put it back before Submit
    observe_similar(tab)
    assert tab.extra["similar_kept"][seen_key("Move for us?", "location.relocate")] is True
