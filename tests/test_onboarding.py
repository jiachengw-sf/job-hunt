"""Onboarding: the local preferences/answers form and its server."""

from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest
import yaml

from jobhunt.models import Answers, Preferences, Profile
from jobhunt.onboarding import form as F
from jobhunt.onboarding import server
from jobhunt.paths import get_paths
from tests.conftest import PERSONAS, PERSONAS_DIR, REPO

RESUMES = REPO / "tests" / "fixtures" / "resumes"


def persona_contact(persona: str) -> dict:
    return yaml.safe_load((PERSONAS_DIR / persona / "answers.yaml").read_text())["identity"]


# --- the form -------------------------------------------------------------------------------


def _post_from_values(vals: dict) -> dict[str, list[str]]:
    """What a browser posts for these rendered values."""
    out: dict[str, list[str]] = {}
    for k, v in vals.items():
        if isinstance(v, list):
            out[k] = v
        elif v not in ("", None):
            out[k] = [str(v)]
    return out


@pytest.mark.parametrize("persona", PERSONAS)
def test_form_round_trip_is_lossless(persona):
    """Opening the form on a persona's files and saving unchanged gives the same data."""
    prefs = yaml.safe_load((PERSONAS_DIR / persona / "preferences.yaml").read_text())
    answers = yaml.safe_load((PERSONAS_DIR / persona / "answers.yaml").read_text())
    vals = F.prefill(prefs, answers, profile=None, contact=None)
    html = F.render(vals, token="t", new_questions=[])
    assert "<script>" in html and 'name="token" value="t"' in html
    p2, a2, errors = F.parse(
        _post_from_values(vals), existing_prefs=prefs, existing_answers=answers, new_questions=[]
    )
    assert not errors and not F.validate(p2, a2)
    assert Preferences.model_validate(p2) == Preferences.model_validate(prefs)
    assert Answers.model_validate(a2) == Answers.model_validate(answers)


def test_form_prefills_from_resume_and_profile():
    prof = Profile.model_validate(
        yaml.safe_load((PERSONAS_DIR / "newgrad-analyst" / "profile.yaml").read_text())
    )
    contact = {
        "name": "Jordan Mockley",
        "email": "j@example.com",
        "phone": "617-555-0145",
        "links": {"github": "https://github.com/x"},
    }
    vals = F.prefill(None, None, profile=prof, contact=contact)
    assert vals["a:identity.legal_first_name"] == "Jordan"
    assert vals["a:identity.email"] == "j@example.com"
    assert vals["a:links.github"] == "https://github.com/x"
    assert vals["a:education.graduation_year"] == "2027"
    assert vals["a:eeo.gender"] == "Decline to self-identify"


def test_form_must_box_and_new_questions():
    form = {
        "p:roles.families": ["data analysis, analytics"],
        "p:roles.families.__must": ["on"],
        "p:location.countries": ["US"],
        "p:pay.min": ["70000"],
        "p:pay.unit": ["annual"],
        "a:work_authorization.US.authorized": ["yes"],
        "a:work_authorization.US.sponsorship_future": ["no"],
        "a:eeo.confirmed": ["on"],
        "nq:0:answer": ["Yes"],
        "nq:1:same": ["yes"],
    }
    nq = [
        {
            "label": "Do you have a valid driver's license?",
            "options": ["Yes", "No"],
            "sensitive": False,
        },
        {
            "label": "Will you require [Company] to sponsor you?",
            "candidate_key": "work_auth.sponsorship",
        },
    ]
    p, a, errors = F.parse(form, existing_prefs=None, existing_answers=None, new_questions=nq)
    assert not errors and not F.validate(p, a)
    pr = Preferences.model_validate(p)
    assert (
        pr.roles.families
        and pr.roles.families.must
        and pr.roles.families.value == ["data analysis", "analytics"]
    )
    assert pr.pay and not pr.pay.must and pr.pay.value.min == 70000
    an = Answers.model_validate(a)
    assert an.work_authorization["US"].authorized == "yes" and an.eeo.confirmed
    by = {e.key: e for e in an.bank}
    lic = next(e for e in an.bank if "driver" in e.question)
    assert lic.answer == "Yes" and lic.options == ["Yes"]
    assert by["work_auth.sponsorship"].wordings == ["Will you require [Company] to sponsor you?"]


def test_form_rejects_bad_values():
    form = {
        "p:timing.earliest_start": ["next spring"],
        "a:work_authorization.US.authorized": ["maybe"],
    }
    _, _, errors = F.parse(form, existing_prefs=None, existing_answers=None, new_questions=[])
    assert len(errors) == 2


# --- the server ----------------------------------------------------------------------------


@pytest.fixture
def running_server(persona_home):
    home = persona_home("first-year-intern")
    paths = get_paths()
    token = "test-token-123"
    httpd = server.make_server(paths, token)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    yield home, paths, token, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=5)


def _req(url: str, data: bytes | None = None, headers: dict | None = None) -> tuple[int, str, dict]:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=5) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # type: ignore[no-untyped-def]
        return None


def test_server_token_host_origin_and_save(running_server):
    home, paths, token, port = running_server
    base = f"http://127.0.0.1:{port}"
    assert _req(f"{base}/?t=wrong")[0] == 403
    assert _req(f"{base}/?t={token}", headers={"Host": "evil.example"})[0] == 403
    code, page, hdrs = _req(f"{base}/?t={token}")
    assert code == 200 and "jobhunt setup" in page
    assert (
        "frame-ancestors" in hdrs.get("Content-Security-Policy", "")
        or hdrs.get("X-Frame-Options") == "DENY"
    )
    prefs = yaml.safe_load((home / "preferences.yaml").read_text())
    answers = yaml.safe_load((home / "answers.yaml").read_text())
    vals = F.prefill(prefs, answers, profile=None, contact=None)
    vals["a:standard.sms_consent"] = "no"
    body = urllib.parse.urlencode(
        {**{k: v for k, v in _post_from_values(vals).items()}, "token": [token]}, doseq=True
    ).encode()
    # a cross-site post is refused
    code, _, _ = _req(
        f"{base}/save",
        body,
        {"Origin": "https://evil.example", "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert code == 403
    code, _, _ = _req(f"{base}/save", urllib.parse.urlencode({"token": "nope"}).encode())
    assert code == 403
    code, _, hdrs = _req(
        f"{base}/save", body, {"Origin": base, "Content-Type": "application/x-www-form-urlencoded"}
    )
    assert code == 303 and "saved=1" in hdrs["Location"]
    saved = Answers.model_validate(yaml.safe_load((home / "answers.yaml").read_text()))
    assert saved.standard.sms_consent == "no"
    assert saved.identity.email == answers["identity"]["email"]
    st = server.read_state(paths)
    assert st["status"] == "saved" and st["saves"] == 1
    # invalid input: not saved, the form comes back with the error
    bad = urllib.parse.urlencode({"token": token, "p:timing.earliest_start": "soon"}).encode()
    code, page, _ = _req(f"{base}/save", bad, {"Origin": base})
    assert code == 400 and "Not saved yet" in page
