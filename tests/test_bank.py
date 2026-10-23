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
    assert r.key == "work_auth.sponsorship_now"  # it asks about now
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


@pytest.mark.parametrize(
    ("label", "key"),
    [
        ("Are you lawfully authorized to work in the U.S.?", "work_auth.authorized"),
        ("Are you legally eligible to work in the country where this job is located?",
         "work_auth.authorized"),
        ("Will you need sponsorship to work in the US now or in the future?",
         "work_auth.sponsorship"),
        ("Would you require visa sponsorship to work at our company in the future?",
         "work_auth.sponsorship_future"),
        ("Do you require visa sponsorship?", "work_auth.sponsorship_now"),
    ],
)  # fmt: skip
def test_clear_rewordings_are_suggested(label, key):
    r = Matcher(Answers()).match(label)
    assert (r.key, r.status) == (key, "candidate")  # suggested; the user still confirms


@pytest.mark.parametrize(
    "label",
    [
        "Are you authorized to work in the US without sponsorship?",
        "Are you legally authorized to work in the US and will you not require sponsorship?",
        "Are you a US citizen or authorized to work without sponsorship?",
    ],
)
def test_combined_questions_are_their_own(label):
    """Two questions in one: never a candidate for either half."""
    r = Matcher(Answers()).match(label)
    assert r.key is None and r.status == "unknown"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Are you willing to relocate to Austin, TX?", "Are you willing to relocate to Seattle?"),
        ("Are you able to work onsite in our San Francisco office 5 days a week?",
         "Are you able to work onsite in our New York office 5 days a week?"),
    ],
)  # fmt: skip
def test_one_confirmed_location_wording_covers_every_city(a, b):
    custom = "Can you relocate to Denver, CO before June?"
    bank = [BankEntry(key="location.relocate_to", question="q", wordings=[custom])]
    m = Matcher(Answers(bank=bank))
    assert m.match("Can you relocate to Boston, MA before June?").status == "confirmed"
    assert m.match(a).key == m.match(b).key is not None


def test_place_names_only_count_in_location_questions():
    from jobhunt.bank.match import placeless

    assert placeless("Are you willing to relocate to Austin, TX?").endswith("relocate to place?")
    assert placeless("Do you have experience in Python?") == "Do you have experience in Python?"
    assert placeless("Can you commute to the Bay Area or Washington, DC?") == (
        "Can you commute to the place or place?"
    )


def test_relocation_and_onsite_preferences_answer_city_questions():
    profile, prefs, answers = _load_persona(PERSONAS_DIR / "newgrad-analyst")
    answers.location.willing_to_relocate = None
    answers.location.relocate_anywhere_in = ["US"]
    answers.location.onsite_full_time = "yes"
    us = AnswerContext(profile, answers, prefs, company="Acme", country="US")
    ca = AnswerContext(profile, answers, prefs, company="Acme", country="CA")
    assert BUILTIN["location.relocate_to"].resolve(us) == "yes"
    assert BUILTIN["location.relocate_to"].resolve(ca) is None  # not where they'd go: ask
    assert BUILTIN["location.relocate"].resolve(us) == "yes"
    assert BUILTIN["location.onsite"].resolve(ca) == "yes"
    m = Matcher(answers)
    assert m.match("Are you open to relocating to Chicago, IL for this role?").key == (
        "location.relocate_to"
    )


def test_student_builtins():
    profile, prefs, answers = _load_persona(PERSONAS_DIR / "first-year-intern")
    e = answers.education
    e.high_school_graduation_year, e.act_score, e.further_study = 2026, "33", "undecided"
    answers.identity.school_email = "student@university.example"
    answers.standard.other_offers = "no"
    ctx = AnswerContext(profile, answers, prefs)
    m = Matcher(answers)
    cases = {
        "What is your high school graduation year?": ("education.high_school_graduation_year",
                                                      "2026"),
        "University email address": ("identity.school_email", "student@university.example"),
        "Which standardized tests have you taken?": ("education.test_type", ["ACT"]),
        "ACT composite score": ("education.act_score", "33"),
        "Standardized test score": ("education.test_score", "33"),
        "SAT score": ("education.sat_score", None),  # never another test's score
        "Do you currently have any other offers?": ("standard.other_offers", "no"),
        "Do you plan to pursue a graduate degree?": ("education.further_study", "undecided"),
    }  # fmt: skip
    for label, (key, value) in cases.items():
        r = m.match(label)
        assert (r.key, r.status) == (key, "exact"), label
        assert BUILTIN[key].resolve(ctx) == value, label
    for k in ("identity.school_email", "education.act_score", "education.test_score"):
        assert BUILTIN[k].sensitive
    e.act_score, e.no_test_scores = None, True
    assert BUILTIN["education.test_type"].resolve(ctx) == "None"
    assert "I have not taken a standardized test" in BUILTIN["education.test_type"].option_aliases(
        "None"
    )
