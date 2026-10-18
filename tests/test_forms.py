"""Form readers: Greenhouse questions API, Lever /apply page, Ashby page data."""

from __future__ import annotations

import json

import pytest

from jobhunt.forms import ashby, greenhouse, lever
from jobhunt.forms.model import FormError, FormSpec
from tests.conftest import REPO

OVERLAY = REPO / "tests" / "fixtures" / "forms" / "overlay"
GH = OVERLAY / "boards-api.greenhouse.io" / "v1" / "boards"
LV = OVERLAY / "jobs.lever.co"
AB = OVERLAY / "jobs.ashbyhq.com"


def gh(board: str, jid: str) -> FormSpec:
    return greenhouse.parse(json.loads((GH / board / "jobs" / f"{jid}.json").read_text()))


def lv(site: str, jid: str) -> FormSpec:
    return lever.parse((LV / site / jid / "apply.html").read_text())


def ab(board: str, jid: str) -> FormSpec:
    return ashby.parse(json.loads((AB / board / jid / "form.json").read_text())["jobPosting"])


def test_greenhouse_parse():
    s = gh("northwind", "1004")
    by = {f.label: f for f in s.fields}
    assert by["First Name"].required and by["First Name"].id == "first_name"
    assert by["Resume/CV"].type == "file"
    assert by["How did you hear about this job?"].options[0] == "Job board"
    assert {f.section for f in s.fields} == {"main", "eeo"}
    assert s.description and "robot" in s.description


def test_lever_parse_reads_cards_and_captcha():
    s = lv("bluefin", "4d1c2e7a-0b6f-4c1e-9a7d-2f3b5c6d7e02")
    by = {f.label: f for f in s.fields}
    assert by["Full name"].required and by["Full name"].id == "name"
    assert by["Current location"].type == "location" and by["Current location"].max_length == 100
    lic = by["Do you have a valid driver's license?"]
    assert lic.type == "radio" and lic.options == ["Yes", "No"] and lic.required
    assert by["Please tell us how you heard about this opportunity."].type == "select"
    assert s.captcha_at_submit


def test_lever_page_without_form_is_an_error():
    with pytest.raises(FormError):
        lever.parse("<html><body>Not found</body></html>")


def test_ashby_parse_main_and_eeo_survey():
    s = ab("quillstack", "9e8d7c6b-5a49-4382-9170-6f5e4d3c2b02")
    sections = {f.label: f.section for f in s.fields}
    assert sections["Gender"] == "eeo"
    by = {f.label: f for f in s.fields}
    assert by["Email"].type == "email" and by["Resume"].type == "file"
    assert by["Do you have a valid driver's license?"].type == "boolean"


def test_ashby_request_guard_allows_only_reads():
    allowed = ashby._allowed
    assert allowed("GET", "https://jobs.ashbyhq.com/acme/123/application")
    assert allowed("POST", "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting")
    for op in ("ApiSubmitSingleApplicationFormAction", "ApiUploadFile", "Anything"):
        assert not allowed("POST", f"https://jobs.ashbyhq.com/api/non-user-graphql?op={op}")
    assert not allowed("POST", "https://jobs.ashbyhq.com/api/submit")
    assert not allowed("PUT", "https://jobs.ashbyhq.com/x")
