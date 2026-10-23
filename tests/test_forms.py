"""Form readers (Greenhouse / Lever / Ashby), the four classes and always-human fields."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobhunt.bank.keys import AnswerContext
from jobhunt.bank.match import Matcher
from jobhunt.fit.evaluate import _load_persona
from jobhunt.forms import ashby, greenhouse, lever
from jobhunt.forms.classify import classify, display_value
from jobhunt.forms.model import FormError, FormSpec
from jobhunt.http import FixtureTransport, HttpClient
from tests.conftest import PERSONAS, PERSONAS_DIR, REPO

OVERLAY = REPO / "tests" / "fixtures" / "forms" / "overlay"
PAGES = REPO / "tests" / "fixtures" / "forms" / "pages"
GH = OVERLAY / "boards-api.greenhouse.io" / "v1" / "boards"
GH_EDU = OVERLAY / "boards.greenhouse.io" / "v1" / "boards"
LV = OVERLAY / "jobs.lever.co"
AB = OVERLAY / "jobs.ashbyhq.com"


def gh(board: str, jid: str) -> FormSpec:
    return greenhouse.parse(
        json.loads((GH / board / "jobs" / f"{jid}.json").read_text(encoding="utf-8"))
    )


def gh_edu(board: str, jid: str) -> FormSpec:
    """A Greenhouse form with its Education block: the page's config, the board's options."""
    page = (PAGES / "job-boards.greenhouse.io" / board / "jobs" / f"{jid}.html").read_text(
        encoding="utf-8"
    )
    m = greenhouse._CONFIG.search(page)
    assert m, "the fixture page carries an education_config"

    def options(kind: str) -> list[str]:
        raw = (GH_EDU / board / "education" / f"{kind}.json").read_text(encoding="utf-8")
        return [i["text"] for i in json.loads(raw)["items"]]

    return greenhouse.parse(
        json.loads((GH / board / "jobs" / f"{jid}.json").read_text(encoding="utf-8")),
        education_config=json.loads(m.group(1)),
        degrees=options("degrees"),
        disciplines=options("disciplines"),
    )


def lv(site: str, jid: str) -> FormSpec:
    return lever.parse((LV / site / jid / "apply.html").read_text(encoding="utf-8"))


def ab(board: str, jid: str) -> FormSpec:
    return ashby.parse(
        json.loads((AB / board / jid / "form.json").read_text(encoding="utf-8"))["jobPosting"]
    )


SPECS = {
    "gh_A": lambda: gh("northwind", "1001"),
    "gh_B": lambda: gh("northwind", "1002"),
    "gh_C": lambda: gh("northwind", "1004"),
    "gh_E": lambda: gh_edu("northwind", "1005"),
    "lv_A": lambda: lv("bluefin", "4d1c2e7a-0b6f-4c1e-9a7d-2f3b5c6d7e01"),
    "lv_B": lambda: lv("driftwood", "7a8b9c0d-1e2f-4a3b-8c4d-5e6f7a8b9c01"),
    "lv_C": lambda: lv("bluefin", "4d1c2e7a-0b6f-4c1e-9a7d-2f3b5c6d7e02"),
    "ab_A": lambda: ab("quillstack", "9e8d7c6b-5a49-4382-9170-6f5e4d3c2b01"),
    "ab_B": lambda: ab("kitewire", "1f2e3d4c-5b6a-4798-8695-a4b3c2d1e001"),
    "ab_C": lambda: ab("quillstack", "9e8d7c6b-5a49-4382-9170-6f5e4d3c2b02"),
}
COMPANY = {"gh": "Northwind Robotics", "lv": "Bluefin Analytics", "ab": "Quillstack"}


def ctx_for(persona: str, company: str) -> tuple[AnswerContext, Matcher, bool]:
    profile, prefs, answers = _load_persona(PERSONAS_DIR / persona)
    ctx = AnswerContext(
        profile, answers, prefs, company=company, country="US", resume=Path("r.pdf")
    )
    return ctx, Matcher(answers), answers.eeo.confirmed


def test_greenhouse_parse():
    s = gh("northwind", "1004")
    by = {f.label: f for f in s.fields}
    assert by["First Name"].required and by["First Name"].id == "first_name"
    assert by["Resume/CV"].type == "file"
    assert by["How did you hear about this job?"].options[0] == "Job board"
    assert {f.section for f in s.fields} == {"main", "eeo"}
    assert s.description and "robot" in s.description


def test_greenhouse_adds_hispanic_question_before_race():
    s = gh("northwind", "1004")
    ids = [f.id for f in s.fields]
    assert ids.index("hispanic_ethnicity") == ids.index("race") - 1
    by = {f.id: f for f in s.fields}
    hisp, race = by["hispanic_ethnicity"], by["race"]
    assert hisp.label == "Are you Hispanic/Latino?" and hisp.section == "eeo"
    assert hisp.options == ["Yes", "No", "Decline To Self Identify"]
    assert hisp.required == race.required and hisp.shown_if is None
    assert race.shown_if and race.shown_if.field == "hispanic_ethnicity"
    assert race.shown_if.options == ["No"]
    # no race question, no Hispanic question
    data = json.loads((GH / "northwind" / "jobs" / "1004.json").read_text(encoding="utf-8"))
    for block in data["compliance"]:
        block["questions"] = [q for q in block["questions"] if q["label"] != "Race"]
    assert "hispanic_ethnicity" not in {f.id for f in greenhouse.parse(data).fields}


def test_greenhouse_hispanic_question_is_eeo():
    ctx, matcher, _ = ctx_for("intl-sponsorship", "Northwind Robotics")
    spec = gh("northwind", "1004")
    c = classify(spec, ctx, matcher, eeo_confirmed=False)
    hisp = next(f for f in c.fields if f.id == "hispanic_ethnicity")
    assert hisp.key == "eeo.hispanic" and hisp.sensitive
    assert hisp.resolution == "human_only"
    assert any("self-identification" in h for h in hisp.human)
    c2 = classify(spec, ctx, matcher, eeo_confirmed=True)
    h2 = next(f for f in c2.fields if f.id == "hispanic_ethnicity")
    assert display_value(h2, ctx, matcher) == "Decline To Self Identify"


def test_greenhouse_education_block_follows_the_page_config():
    """Education sits outside questions[]: the page's education_config says which parts show."""
    s = gh_edu("northwind", "1005")
    by = {f.id: f for f in s.fields}
    assert s.education and s.education.parts == {
        "school_name": True, "degree": True, "discipline": True, "start_year": False,
        "end_month": True, "end_year": True,
    }  # fmt: skip
    assert "start-month--0" not in by  # hidden on this page
    assert by["school--0"].required and by["school--0"].options == []  # searched on the page
    assert by["degree--0"].options[:2] == ["Accelerated Master's", "Advanced Certificate"]
    assert "Statistics" in by["discipline--0"].options
    assert by["end-month--0"].options[4] == "May" and by["end-year--0"].type == "number"
    assert not by["start-year--0"].required


def test_greenhouse_education_without_page_config_assumes_every_part():
    data = json.loads((GH / "northwind" / "jobs" / "1005.json").read_text(encoding="utf-8"))
    req = greenhouse.parse(data)
    edu = [f for f in req.fields if f.id.endswith("--0")]
    assert len(edu) == 7 and all(f.required for f in edu)
    data["education"] = "education_optional"
    assert not any(f.required for f in greenhouse.parse(data).fields if f.id.endswith("--0"))
    del data["education"]  # education_hidden: the API leaves the key out
    s = greenhouse.parse(data)
    assert s.education is None and not any(f.id.endswith("--0") for f in s.fields)


def test_greenhouse_fetch_reads_the_page_config_and_option_lists():
    """fetch() GETs the public job page and the board's degree and discipline lists."""
    roots = [FixtureTransport(OVERLAY), FixtureTransport(PAGES)]

    def transport(url, headers, timeout):
        for t in roots:
            r = t(url, headers, timeout)
            if r.status != 404:
                return r
        return r

    client = HttpClient(user_agent="test", min_host_interval=0, transport=transport)
    s = greenhouse.fetch(client, "northwind", "1005")
    assert s.model_dump() == gh_edu("northwind", "1005").model_dump() | {"url": s.url}
    greenhouse.fetch(client, "northwind", "1005")  # option lists are read once per client
    calls = roots[0].calls + roots[1].calls
    assert sum("education/degrees" in c for c in calls) == 1
    plain = HttpClient(user_agent="test", min_host_interval=0, transport=transport)
    calls_before = len(roots[0].calls) + len(roots[1].calls)
    greenhouse.fetch(plain, "northwind", "1001")  # no education: no extra requests
    assert len(roots[0].calls) + len(roots[1].calls) - calls_before == 1


@pytest.mark.parametrize("persona", PERSONAS)
def test_unresolved_education_is_never_ready(persona):
    """Without the page config every part is required; an entry with no start date can't be
    Ready, and neither can a degree or discipline the form doesn't list."""
    data = json.loads((GH / "northwind" / "jobs" / "1005.json").read_text(encoding="utf-8"))
    spec = greenhouse.parse(data, degrees=["Bachelor's Degree", "Master's Degree"],
                            disciplines=["Biology"])  # fmt: skip
    ctx, matcher, eeo = ctx_for(persona, COMPANY["gh"])
    c = classify(spec, ctx, matcher, eeo_confirmed=eeo)
    assert c.cls != "ready"
    needs = {f.id: f.resolution for f in c.needs}
    assert needs["discipline--0"] == "option_mismatch"
    if not ctx.profile.education[0].start:
        assert needs["start-month--0"] == needs["start-year--0"] == "unanswered"


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


@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize("name", sorted(SPECS))
def test_classes_follow_the_rules(persona, name):
    spec = SPECS[name]()
    ctx, matcher, eeo_ok = ctx_for(persona, COMPANY[name[:2]])
    c = classify(spec, ctx, matcher, eeo_confirmed=eeo_ok)
    tpl = name[-1]
    if tpl == "B":
        assert c.cls == "essay" and c.essays
    elif tpl == "C":
        assert c.cls == "needs_answers"
        assert any("driver" in f.label for f in c.needs)
    else:
        # template A: Ready exactly when every required field has a confident answer
        blocking = [
            f for f in c.fields if f.required and f.resolution not in ("answered", "human_only")
        ]
        assert (c.cls == "ready") == (not blocking), [(f.label, f.resolution) for f in blocking]
    # outputs never carry values
    dumped = c.model_dump_json()
    _, _, answers = _load_persona(PERSONAS_DIR / persona)
    for secret in (answers.identity.email, answers.identity.phone):
        if secret:
            assert secret not in dumped


def test_intl_persona_ready_and_values_fit_options():
    ctx, matcher, eeo = ctx_for("intl-sponsorship", "Northwind Robotics")
    c = classify(gh("northwind", "1001"), ctx, matcher, eeo_confirmed=eeo)
    assert c.cls == "ready", [(f.label, f.resolution, f.reason) for f in c.fields]
    by = {f.label: f for f in c.fields}
    spons = next(f for label, f in by.items() if label.startswith("Will you now or in the future"))
    assert display_value(spons, ctx, matcher) == "Yes"
    heard = by["How did you hear about this job?"]
    assert display_value(heard, ctx, matcher) == "Job board"


def test_always_human_fields():
    ctx, matcher, _ = ctx_for("intl-sponsorship", "Quillstack")
    c = classify(
        ab("quillstack", "9e8d7c6b-5a49-4382-9170-6f5e4d3c2b01"), ctx, matcher, eeo_confirmed=False
    )
    human = {f.label: f.human for f in c.fields if f.human}
    assert any(
        "knockout" in h for h in human["Are you legally authorized to work in the United States?"]
    )
    assert any("self-identification" in h for h in human["Gender"])
    gender = next(f for f in c.fields if f.label == "Gender")
    assert gender.resolution == "human_only"  # not filled until the user reviews EEO
    # once the user presets EEO, Decline is filled from the form's own option text
    c2 = classify(
        ab("quillstack", "9e8d7c6b-5a49-4382-9170-6f5e4d3c2b01"), ctx, matcher, eeo_confirmed=True
    )
    g2 = next(f for f in c2.fields if f.label == "Veteran Status")
    assert (
        display_value(g2, ctx, matcher) == "I decline to self-identify for protected veteran status"
    )
    lv_c = classify(
        lv("bluefin", "4d1c2e7a-0b6f-4c1e-9a7d-2f3b5c6d7e01"), ctx, matcher, eeo_confirmed=False
    )
    radios = [f for f in lv_c.fields if f.type == "radio"]
    assert radios and all(any("Lever" in h for h in f.human) for f in radios)


def test_attestation_is_human_only():
    from jobhunt.forms.model import FormField

    spec = FormSpec(
        ats="greenhouse",
        fields=[
            FormField(
                id="q1",
                label="I certify that my answers are true and complete.",
                type="checkbox",
                required=True,
            )
        ],
    )
    ctx, matcher, _ = ctx_for("first-year-intern", "Acme")
    c = classify(spec, ctx, matcher, eeo_confirmed=False)
    assert c.cls == "ready" and c.fields[0].resolution == "human_only" and c.fields[0].human


def test_unanswered_known_question_needs_answers():
    from jobhunt.forms.model import FormField

    spec = FormSpec(
        ats="greenhouse",
        fields=[
            FormField(
                id="q1", label="Do you hold a security clearance?", type="text", required=True
            )
        ],
    )
    ctx, matcher, _ = ctx_for("first-year-intern", "Acme")
    ctx.answers.standard.security_clearance = None
    c = classify(spec, ctx, matcher, eeo_confirmed=False)
    assert c.cls == "needs_answers" and c.fields[0].resolution == "unanswered"


def test_char_limit_is_checked():
    from jobhunt.forms.model import FormField

    spec = FormSpec(
        ats="lever",
        fields=[
            FormField(id="org", label="Current company", type="text", required=True, max_length=3)
        ],
    )
    ctx, matcher, _ = ctx_for("senior-backend", "Acme")
    c = classify(spec, ctx, matcher, eeo_confirmed=False)
    assert c.fields[0].resolution == "too_long" and c.cls == "needs_answers"
