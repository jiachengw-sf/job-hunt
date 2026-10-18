"""Answer bank: normalization, confirmed reuse, select options, sensitive labels."""

from __future__ import annotations

import pytest

from jobhunt.bank.keys import BUILTIN, AnswerContext, is_sensitive_label
from jobhunt.bank.match import Matcher, choose_option, company_template, normalize, seed_wordings
from jobhunt.fit.evaluate import _load_persona
from jobhunt.models import Answers
from jobhunt.models.answers import BankEntry
from tests.conftest import PERSONAS, PERSONAS_DIR


def test_normalize_strips_markup_filler_and_company():
    assert normalize("<p>Please select your <b>Gender</b> (required)*</p>") == "select your gender"
    assert (
        normalize(
            "Have you ever been employed by Acme Corp or an Acme Corp affiliate?", "Acme Corp"
        )
        == "have you ever been employed by company or an company affiliate"
    )
    assert normalize(company_template("Why Acme?", "Acme")) == "why company"


def test_every_seed_key_is_builtin():
    assert set(seed_wordings()) <= set(BUILTIN)


def test_exact_curated_wording_and_field_names():
    m = Matcher(Answers())
    r = m.match("Are you legally authorized to work in the United States?")
    assert (r.key, r.status) == ("work_auth.authorized", "exact")
    assert m.match("anything", field_name="first_name").key == "identity.first_name"
    assert m.match("Legal Name", field_name="_systemfield_name").key == "identity.full_name"


def test_new_wording_is_only_a_candidate_until_confirmed():
    label = "Do you require sponsorship now?"
    m = Matcher(Answers())
    r = m.match(label)
    assert r.status == "candidate" and not r.confident
    assert r.key == "work_auth.sponsorship"
    confirmed = Answers(
        bank=[BankEntry(key="work_auth.sponsorship_now", question="q", wordings=[label])]
    )
    r2 = Matcher(confirmed).match(label)
    assert (r2.key, r2.status) == ("work_auth.sponsorship_now", "confirmed")


def test_confirmed_company_template_carries_to_other_companies():
    wording = "Will you require [Company] to sponsor you this year?"
    a = Answers(bank=[BankEntry(key="work_auth.sponsorship_now", question="q", wordings=[wording])])
    r = Matcher(a).match("Will you require Globex to sponsor you this year?", company="Globex")
    assert (r.key, r.status) == ("work_auth.sponsorship_now", "confirmed")


def test_unrelated_label_is_unknown():
    r = Matcher(Answers()).match("What is your favorite programming paradigm and why?")
    assert r.key is None and r.status == "unknown"


def test_custom_bank_question_matches():
    a = Answers(
        bank=[
            BankEntry(
                key="custom.license", question="Do you have a driver's license?", answer="Yes"
            )
        ]
    )
    r = Matcher(a).match("Do you have a driver's license?")
    assert (r.key, r.status) == ("custom.license", "confirmed")


def test_choose_option_yes_no_and_ambiguity():
    assert choose_option(["yes", "Yes"], ["Yes", "No"], yesno=True) == "Yes"
    assert choose_option(["yes"], ["Yes, I am authorized", "No, I am not"], yesno=True) == (
        "Yes, I am authorized"
    )
    # two options start with "Yes": never guess between them
    assert choose_option(["yes"], ["Yes, now", "Yes, in the future", "No"], yesno=True) is None
    assert choose_option(["Job board"], ["LinkedIn", "Job Board", "Other"]) == "Job Board"
    assert choose_option(["Podcast"], ["LinkedIn", "Other"]) is None


def test_sensitive_labels():
    for s in ["Will you require visa sponsorship?", "Gender", "What are your salary expectations?",
              "Have you ever been convicted of a felony?", "Are you a U.S. citizen?",
              "Are you currently in F-1 status?", "Will you need H-1B transfer?"]:  # fmt: skip
        assert is_sensitive_label(s), s
    assert not is_sensitive_label("How did you hear about us?")
    assert not is_sensitive_label("What is your option for start dates?")  # "opt" as a word only


@pytest.mark.parametrize("persona", PERSONAS)
def test_sponsorship_combines_now_and_future(persona):
    profile, prefs, answers = _load_persona(PERSONAS_DIR / persona)
    ctx = AnswerContext(profile, answers, prefs, company="Acme", country="US")
    wa = answers.work_authorization.get("US")
    v = BUILTIN["work_auth.sponsorship"].resolve(ctx)
    if wa and "yes" in (wa.sponsorship_now, wa.sponsorship_future):
        assert v == "yes"
    elif wa and wa.sponsorship_now == "no" and wa.sponsorship_future == "no":
        assert v == "no"
    else:
        assert v is None  # unanswered: never assumed


def test_previously_employed_needs_a_known_employer_list():
    from jobhunt.models import Profile

    ctx = AnswerContext(Profile(), Answers(), company="Acme")
    assert BUILTIN["experience.previously_employed"].resolve(ctx) is None
    a = Answers.model_validate({"experience": {"prior_employers": ["Acme, Inc."]}})
    assert (
        BUILTIN["experience.previously_employed"].resolve(
            AnswerContext(Profile(), a, company="ACME")
        )
        == "yes"
    )
    assert (
        BUILTIN["experience.previously_employed"].resolve(
            AnswerContext(Profile(), a, company="Globex")
        )
        == "no"
    )


def test_gpa_respects_disclosure_threshold():
    from jobhunt.models import Profile

    a = Answers.model_validate({"education": {"gpa": 3.1, "gpa_disclose_at_or_above": 3.3}})
    assert BUILTIN["education.gpa"].resolve(AnswerContext(Profile(), a)) is None
    a2 = Answers.model_validate({"education": {"gpa": 3.6, "gpa_disclose_at_or_above": 3.3}})
    assert BUILTIN["education.gpa"].resolve(AnswerContext(Profile(), a2)) == "3.6"
