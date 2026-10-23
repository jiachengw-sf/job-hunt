"""Education blocks: degree levels, names and dates matched to a form's own options."""

from __future__ import annotations

import pytest

from jobhunt.forms import education
from jobhunt.forms.education import degree_level, month_name, pick_degree, pick_name
from tests.fixtures.forms.make_forms import DEGREES


@pytest.mark.parametrize(
    ("text", "level"),
    [
        ("BS", "bachelor"), ("B.S.", "bachelor"), ("BASc", "bachelor"), ("BEng", "bachelor"),
        ("Bachelor of Arts", "bachelor"), ("Bachelor's Degree", "bachelor"),
        ("MS", "master"), ("M.Eng.", "master"), ("Master's Degree", "master"),
        ("MBA", "mba"), ("Master's of Business Administration", "mba"),
        ("PhD", "phd"), ("Ph.D.", "phd"), ("Doctor of Philosophy (Ph.D.)", "phd"),
        ("Doctorate", "doctorate"), ("J.D.", "jd"), ("Doctor of Medicine (M.D.)", "md"),
        ("Associate's Degree", "associate"), ("High School", "high_school"),
        ("Certificate", None), ("Other", None), (None, None),
    ],
)  # fmt: skip
def test_degree_level(text, level):
    assert degree_level(text) == level


@pytest.mark.parametrize(
    ("value", "chosen"),
    [
        ("BS", "Bachelor's Degree"), ("Bachelors", "Bachelors"),  # the form's own text wins
        ("MS", "Master's Degree"), ("MBA", "Master of Business Administration (M.B.A.)"),
        ("PhD", "Doctor of Philosophy (Ph.D.)"), ("JD", "Juris Doctor (J.D.)"),
        ("AS", "Associate's Degree"), ("Certificate", "Certificate"), ("Diploma", None),
    ],
)  # fmt: skip
def test_pick_degree_takes_the_same_level_never_a_narrower_one(value, chosen):
    assert pick_degree(value, DEGREES) == chosen


def test_pick_degree_falls_back_only_to_a_broader_level():
    assert pick_degree("PhD", ["Bachelors", "Doctorate"]) == "Doctorate"
    assert pick_degree("MBA", ["Bachelors", "Masters"]) == "Masters"
    assert pick_degree("MS", ["Accelerated Master's", "Bachelors"]) is None
    assert pick_degree("BS", []) is None


def test_pick_name_needs_the_same_name():
    opts = ["Computer Science", "Computer Science & Engineering", "Mathematics", "Statistics"]
    assert pick_name("computer science", opts) == "Computer Science"
    assert pick_name("Computer Science and Engineering", opts) == "Computer Science & Engineering"
    assert pick_name("CS", opts) == "Computer Science"
    assert pick_name("Data Science, Statistics", opts) == "Statistics"  # a listed major
    assert pick_name("Computer", opts) is None  # never the closest-sounding one
    assert pick_name("The Riverside University", ["Riverside University"]) == "Riverside University"
    assert pick_name("Riverside", ["Riverside University", "Riverside College"]) is None


def test_dates():
    assert month_name("2028-05") == "May" and month_name("2028") is None
    assert education.year_of("2028-05") == "2028" and education.year_of(None) is None


def test_part_of_reads_entry_ids():
    part, n = education.part_of("end-month--2") or (None, None)
    assert part and part.name == "end_month" and n == 2
    assert education.part_of("question_123") is None and education.part_of("foo--1") is None
