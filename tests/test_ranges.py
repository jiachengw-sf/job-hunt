"""Range options: a single answer picks the one option that contains it, or none."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobhunt.bank.keys import BUILTIN, AnswerContext
from jobhunt.bank.match import Matcher
from jobhunt.bank.ranges import date_bounds, pick_date, pick_number
from jobhunt.fit.evaluate import _load_persona
from jobhunt.forms.classify import classify, fill_value
from jobhunt.forms.model import FormField, FormSpec
from tests.conftest import PERSONAS_DIR

GPA = ["Below 3.0", "3.0 - 3.5", "3.51 - 3.9", "3.91 - 4.0"]
GRAD = ["January 2027 - July 2027", "August 2027 - December 2027", "January 2028 - July 2028"]
YEARS = ["Less than 1 year", "1-2 years", "3-5 years", "6+ years"]
PAY = ["Under $50,000", "$50k - $75k", "$75,001 - $100,000", "More than $100,000"]


@pytest.mark.parametrize(
    ("value", "options", "chosen"),
    [
        ("4.0", GPA, "3.91 - 4.0"), ("3.5", GPA, "3.0 - 3.5"), ("2.9", GPA, "Below 3.0"),
        ("3.0", GPA, "3.0 - 3.5"),  # "Below 3.0" excludes 3.0
        ("3.5", ["3.0 - 3.5", "3.5 - 4.0"], None),  # listed twice: the user decides
        ("4.0", ["4.0", "3.5 - 4.0"], "4.0"),  # the equal option wins
        ("0.5", YEARS, "Less than 1 year"), ("4", YEARS, "3-5 years"), ("6", YEARS, "6+ years"),
        ("2.5", YEARS, None),  # falls between two options
        ("60000", PAY, "$50k - $75k"), ("$100,000", PAY, "$75,001 - $100,000"),
        ("120k", PAY, "More than $100,000"), ("Prefer not to say", PAY, None),
        ("5", ["5 or more", "4 or fewer"], "5 or more"),
        ("4", ["5 or more", "4 or fewer"], "4 or fewer"),
        ("2028", ["2027", "2028", "2029 or later"], "2028"),
    ],
)  # fmt: skip
def test_pick_number(value, options, chosen):
    assert pick_number(value, options) == chosen


@pytest.mark.parametrize(
    ("value", "options", "chosen"),
    [
        ("2028-05", GRAD, "January 2028 - July 2028"),
        ("2027-12", GRAD, "August 2027 - December 2027"),
        ("2027-07-15", GRAD, "January 2027 - July 2027"),  # a day: its month
        ("2029-05", GRAD, None),
        ("2028-05", ["Jan 2028 – Jul 2028", "Aug 2028 – Dec 2028"], "Jan 2028 – Jul 2028"),
        ("2028-05", ["05/2028", "12/2028"], "05/2028"),
        ("2028-05", ["May 2028", "December 2028"], "May 2028"),
        ("2025-06", ["Before 2026", "2026", "2027 or later"], "Before 2026"),
        ("2030-05", ["Before 2026", "2026", "2027 or later"], "2027 or later"),
        ("2028-05", ["Spring 2028", "Fall 2028"], None),  # a season's months aren't stated
        ("2028-05", ["2027 - 2028", "2029 - 2030"], "2027 - 2028"),
    ],
)  # fmt: skip
def test_pick_date(value, options, chosen):
    assert pick_date(value, options) == chosen


def test_no_date_in_option():
    assert date_bounds("Other") is None and pick_date("not a date", GRAD) is None


@pytest.mark.parametrize(
    "key",
    ["education.gpa", "education.graduation", "education.graduation_year", "experience.years",
     "compensation.expected", "availability.start_date", "availability.end_date",
     "availability.hours_per_week"],
)  # fmt: skip
def test_every_builtin_that_can_be_a_range_matches_ranges(key):
    assert BUILTIN[key].pick in (pick_number, pick_date)


def test_range_options_resolve_and_fill_with_the_forms_own_text():
    """The report's cases: graduation 2028-05 and GPA 4.0 used to be option_mismatch."""
    profile, prefs, answers = _load_persona(PERSONAS_DIR / "first-year-intern")
    answers.education.graduation_year, answers.education.graduation_month = 2028, 5
    answers.education.gpa = 4.0
    ctx = AnswerContext(profile, answers, prefs, company="Acme", country="US", resume=Path("r"))
    spec = FormSpec(
        ats="greenhouse",
        fields=[
            FormField(id="q1", label="Graduation date", type="select", required=True, options=GRAD),
            FormField(id="q2", label="Cumulative GPA", type="select", required=True, options=GPA),
        ],
    )
    matcher = Matcher(answers)
    c = classify(spec, ctx, matcher, eeo_confirmed=True)
    by = {f.id: f for f in c.fields}
    assert by["q1"].resolution == by["q2"].resolution == "answered", c.fields
    assert fill_value(by["q1"], ctx, matcher) == "January 2028 - July 2028"
    assert fill_value(by["q2"], ctx, matcher) == "3.91 - 4.0"
