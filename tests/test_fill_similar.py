"""Fewer repeat questions: similar wordings fill from the user's own answer for review, the agent
can map new wordings (code checks them), and submitting teaches the bank."""

from __future__ import annotations

import json

import yaml

from jobhunt.bank.learn import add_wordings, learn_from_submission, reviewed_pairs, seen_key
from jobhunt.bank.match import Match, Matcher
from jobhunt.errors import ExitCode
from jobhunt.forms.classify import classify, similar_is_clear
from jobhunt.forms.model import FormField, FormSpec
from jobhunt.paths import get_paths
from jobhunt.prefill.values import AnswerSheet, FillField, build_sheet, write_sheet
from tests.test_forms import ctx_for

SPONSOR = "Will you, now or in the future, need [Company] to sponsor an employment visa for you?"


def spec_of(*fields: FormField) -> FormSpec:
    return FormSpec(ats="greenhouse", url="https://example.test/apply", fields=list(fields))


def yesno(fid: str, label: str, required: bool = True) -> FormField:
    return FormField(id=fid, label=label, type="select", required=required, options=["Yes", "No"])


def test_a_clear_similar_wording_fills_flagged_for_review():
    ctx, matcher, eeo = ctx_for("first-year-intern", "Northwind")
    label = "When is your earliest start date?"
    assert matcher.match(label, company="Northwind").status == "candidate"
    text = FormField(id="q1", label=label, type="text", required=True)
    asked = classify(spec_of(text), ctx, matcher, eeo_confirmed=eeo)
    assert asked.fields[0].resolution == "candidate"  # the default Matcher still asks first

    matcher.fill_similar = True
    c = classify(spec_of(text), ctx, matcher, eeo_confirmed=eeo)
    fr = c.fields[0]
    assert fr.resolution == "answered" and fr.match == "candidate"
    assert any(h.startswith("similar question") for h in fr.human)
    sheet = build_sheet(1, spec_of(text), c, ctx, matcher, "2026-11-06T00:00:00Z")
    ff = sheet.fields[0]
    assert ff.action == "fill" and ff.value and ff.similar


def test_a_sensitive_similar_wording_is_asked_never_filled():
    ctx, matcher, eeo = ctx_for("intl-sponsorship", "Northwind")
    matcher.fill_similar = True
    label = SPONSOR.replace("[Company]", "Northwind")
    assert matcher.match(label, company="Northwind").status == "candidate"
    fr = classify(spec_of(yesno("q1", label)), ctx, matcher, eeo_confirmed=eeo).fields[0]
    assert fr.resolution == "candidate" and fr.sensitive


def test_an_ambiguous_match_with_different_answers_still_asks():
    ctx, matcher, _ = ctx_for("intl-sponsorship", "Northwind")
    m = Match("work_auth.sponsorship_now", "candidate", 0.70, "similarity", "x",
              alt_key="work_auth.sponsorship_future", alt_score=0.68)  # fmt: skip
    assert not similar_is_clear(m, ctx, matcher, sensitive=False)  # now: no; future: yes
    m.alt_score = 0.5
    assert similar_is_clear(m, ctx, matcher, sensitive=False)


def test_sensitive_and_eeo_never_fill_by_similarity():
    ctx, matcher, _ = ctx_for("first-year-intern", "Northwind")
    near = Match("work_auth.sponsorship", "candidate", 0.99, "similarity", "x")
    assert not similar_is_clear(near, ctx, matcher, sensitive=True)
    agent = Match("work_auth.sponsorship", "candidate", 1.0, "agent", "x")
    assert not similar_is_clear(agent, ctx, matcher, sensitive=True)
    eeo = Match("eeo.gender", "candidate", 0.99, "similarity", "x")
    assert not similar_is_clear(eeo, ctx, matcher, sensitive=True)


def test_answers_map_accepts_same_questions_and_refuses_the_rest(cli, persona_home):
    home = persona_home("first-year-intern")
    code, env, _ = cli(
        "answers", "map",
        "Is [Company] able to rely on you being available all summer?", "start.summer_availability",
        "Do you currently require sponsorship?", "work_auth.authorized",
        "What is your gender?", "eeo.gender",
        "Anything", "no.such.key",
    )  # fmt: skip
    assert code == ExitCode.OK, env
    d = env["data"]
    assert [m["key"] for m in d["mapped"]] == ["start.summer_availability"]
    reasons = {r["key"]: r["reason"] for r in d["rejected"]}
    assert "different things" in reasons["work_auth.authorized"]
    assert "EEO" in reasons["eeo.gender"]
    assert "no question" in reasons["no.such.key"]
    raw = yaml.safe_load((home / "answers.yaml").read_text(encoding="utf-8"))
    e = next(x for x in raw["bank"] if x["key"] == "start.summer_availability")
    assert e["proposed"] == ["Is [Company] able to rely on you being available all summer?"]
    # the agent never needed the answer, and a mapped label now fills for review
    assert "answer" not in json.dumps(d["mapped"]).lower()


def test_a_proposed_label_fills_for_review():
    ctx, matcher, eeo = ctx_for("first-year-intern", "Acme")
    e = next(x for x in matcher.answers.bank if x.key == "start.summer_availability")
    e.proposed.append("Can [Company] count on you all summer?")
    m = Matcher(matcher.answers, fill_similar=True)
    hit = m.match("Can Acme count on you all summer?", company="Acme")
    assert (hit.key, hit.status, hit.via) == ("start.summer_availability", "candidate", "agent")
    c = classify(
        spec_of(yesno("q", "Can Acme count on you all summer?")), ctx, m, eeo_confirmed=eeo
    )
    assert c.fields[0].resolution == "answered"


def test_submitting_teaches_the_bank(persona_home, tmp_path):
    home = persona_home("first-year-intern")
    paths = get_paths()
    sheet = AnswerSheet(job_id=1, ats="greenhouse", url=None, built_at="t", fields=[
        FillField(id="a", label="Can Acme count on you all summer?", type="select", required=True,
                  section="main", key="start.summer_availability", action="fill", value="Yes",
                  similar=True),
        FillField(id="b", label="First Name", type="text", required=True, section="main",
                  key="identity.first_name", action="fill", value="Riley"),
    ])  # fmt: skip
    p = tmp_path / "answers.json"
    write_sheet(p, sheet)
    kept = {seen_key("Can Acme count on you all summer?", "start.summer_availability"): True}
    assert learn_from_submission(paths, [p], "Acme") == []  # never read back: not learned
    assert learn_from_submission(paths, [p], "Acme", kept) == ["start.summer_availability"]
    raw = yaml.safe_load((home / "answers.yaml").read_text(encoding="utf-8"))
    e = next(x for x in raw["bank"] if x["key"] == "start.summer_availability")
    assert "Can [Company] count on you all summer?" in e["wordings"]
    assert "Riley" not in (home / "answers.yaml").read_text(encoding="utf-8").split("bank:")[1]
    # learned once: the same label isn't added twice, and now matches exactly
    assert learn_from_submission(paths, [p], "Acme", kept) == []
    from jobhunt.models import Answers

    m = Matcher(Answers.model_validate(raw)).match("Can Globex count on you all summer?",
                                                   company="Globex")  # fmt: skip
    assert m.status == "confirmed"


def test_add_wordings_promotes_a_proposed_label():
    raw = {"bank": [{"key": "start.summer_availability", "question": "q",
                     "proposed": ["Around all summer?"]}]}  # fmt: skip
    assert add_wordings(raw, [("Around all summer?", "start.summer_availability")])
    e = raw["bank"][0]
    assert e["wordings"] == ["Around all summer?"] and "proposed" not in e


def test_a_changed_or_sensitive_field_is_not_learned():
    def ff(fid, label, key):
        return FillField(id=fid, label=label, type="select", required=True, section="main",
                         key=key, action="fill", value="Yes", similar=True)  # fmt: skip

    sheet = AnswerSheet(job_id=1, ats="greenhouse", url=None, built_at="t", fields=[
        ff("a", "Can Acme count on you all summer?", "start.summer_availability"),
        ff("b", "Open to moving for Acme?", "location.relocate"),
        ff("c", "Acme visa needs?", "work_auth.sponsorship"),
    ])  # fmt: skip
    kept = {
        seen_key("Can Acme count on you all summer?", "start.summer_availability"): True,
        seen_key("Open to moving for Acme?", "location.relocate"): False,  # the user changed it
        seen_key("Acme visa needs?", "work_auth.sponsorship"): True,
    }
    assert reviewed_pairs([sheet], "Acme", kept) == [
        ("Can [Company] count on you all summer?", "start.summer_availability")
    ]
