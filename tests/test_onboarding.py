"""Onboarding: resume -> draft profile, profile edits, the local form and its server."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
import yaml

from jobhunt.errors import ExitCode
from jobhunt.models import Answers, Preferences, Profile
from jobhunt.onboarding import form as F
from jobhunt.onboarding import resume as rs
from jobhunt.onboarding import server
from jobhunt.paths import get_paths
from tests.conftest import PERSONAS, PERSONAS_DIR, REPO

RESUMES = REPO / "tests" / "fixtures" / "resumes"


def persona_contact(persona: str) -> dict:
    return yaml.safe_load((PERSONAS_DIR / persona / "answers.yaml").read_text(encoding="utf-8"))[
        "identity"
    ]


@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize("ext", ["pdf", "docx", "md"])
def test_resume_text_contact_and_draft(persona, ext):
    path = PERSONAS_DIR / persona / "resume.md" if ext == "md" else RESUMES / f"{persona}.{ext}"
    text = rs.extract_text(path)
    c = rs.find_contact(text)
    ident = persona_contact(persona)
    assert c.email == ident["email"]
    assert c.phone and c.phone[-4:] == ident["phone"][-4:]
    assert c.name and c.name.split()[0] == ident["legal_first_name"]
    red = rs.redact(text, c)
    assert ident["email"] not in red and c.phone not in red and c.name not in red
    draft = rs.draft_profile(text, c, path.name)
    truth = Profile.model_validate(
        yaml.safe_load((PERSONAS_DIR / persona / "profile.yaml").read_text(encoding="utf-8"))
    )
    assert [e["school"] for e in draft["education"]] == [e.school for e in truth.education]
    assert [(e["org"], e["title"]) for e in draft["experience"]] == [
        (e.org, e.title) for e in truth.experience
    ]
    assert draft["skills"]


def test_unsupported_resume_format(tmp_path):
    f = tmp_path / "cv.rtf"
    f.write_text("{\\rtf1}", encoding="utf-8")
    from jobhunt.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        rs.extract_text(f)


def test_resume_add_profile_confirm_and_set(cli, isolated_home):
    code, env, _ = cli("resume", "add", str(RESUMES / "senior-backend.pdf"))
    assert code == ExitCode.OK, env
    d = env["data"]
    assert d["contact_found"]["email"] and d["contact_found"]["phone"]
    blob = json.dumps(env)
    ident = persona_contact("senior-backend")
    assert ident["email"] not in blob and ident["phone"][-4:] not in blob
    paths = get_paths()
    assert (paths.resume_dir / "senior-backend.pdf").exists()
    redacted = Path(d["redacted_text"]).read_text(encoding="utf-8")
    assert ident["email"] not in redacted
    # contact details wait for the form, owner-only
    contact = json.loads(server.contact_path(paths).read_text(encoding="utf-8"))
    assert contact["email"] == ident["email"]
    # an unreviewed draft can't be confirmed
    code, env, _ = cli("profile", "confirm")
    assert code == ExitCode.CONFIG and env["error"]["details"]["placeholders"]
    for p in env["error"]["details"]["placeholders"]:
        code, e2, _ = cli("profile", "set", "--draft", p, "Reviewed by the user")
        assert code == ExitCode.OK, e2
    code, env, _ = cli("profile", "confirm")
    assert code == ExitCode.OK, env
    prof = Profile.model_validate(yaml.safe_load(paths.profile.read_text(encoding="utf-8")))
    assert prof.resumes[0].file == "senior-backend.pdf" and prof.resumes[0].default
    # set: list index, append, list literal, validation
    code, env, _ = cli("profile", "set", "experience[0].title", "Principal Backend Engineer")
    assert code == ExitCode.OK and env["data"]["changed"]["before"] == "Senior Backend Engineer"
    code, _, _ = cli("profile", "set", "--append", "skills.skills", "Rust")
    assert code == ExitCode.OK
    code, _, _ = cli("profile", "set", "--append", "skills.tools", "Docker")
    code, _, _ = cli("profile", "set", "skills.tools", "null")  # removes the key
    assert code == ExitCode.OK
    code, _, _ = cli("profile", "set", "projects", "[not, a, project]")
    assert code == ExitCode.VALIDATION
    code, _, _ = cli("profile", "set", "experience[9].title", "x")
    assert code == ExitCode.VALIDATION
    prof = Profile.model_validate(yaml.safe_load(paths.profile.read_text(encoding="utf-8")))
    assert prof.experience[0].title == "Principal Backend Engineer"
    assert "Rust" in prof.skills["skills"] and "tools" not in prof.skills
    # a second resume joins the confirmed profile instead of redrafting; the default stays
    code, env, _ = cli(
        "resume", "add", str(RESUMES / "senior-backend.docx"), "--label", "docx copy"
    )
    assert code == ExitCode.OK and env["data"]["draft"] is None
    assert env["data"]["resumes"] == ["senior-backend.pdf", "senior-backend.docx"]
    prof = Profile.model_validate(yaml.safe_load(paths.profile.read_text(encoding="utf-8")))
    assert [r.file for r in prof.resumes if r.default] == ["senior-backend.pdf"]
    code, env, _ = cli("resume", "add", str(RESUMES / "senior-backend.docx"), "--default")
    prof = Profile.model_validate(yaml.safe_load(paths.profile.read_text(encoding="utf-8")))
    assert [(r.file, r.default, r.label) for r in prof.resumes] == [
        ("senior-backend.pdf", False, None),
        ("senior-backend.docx", True, "docx copy"),  # its label is kept
    ]


def test_several_resumes_before_confirming_keep_the_draft_and_its_list(cli, isolated_home):
    """Each `resume add` used to replace the draft, its resume list and its default."""
    paths = get_paths()
    cli("resume", "add", str(RESUMES / "newgrad-analyst.pdf"), "--label", "analytics")
    code, e2, _ = cli("profile", "set", "--draft", "headline", "Reviewed headline")
    assert code == ExitCode.OK, e2
    code, env, _ = cli("resume", "add", str(RESUMES / "intl-sponsorship.pdf"), "--not-default")
    assert code == ExitCode.OK, env
    assert env["data"]["draft_kept"] and "--redraft" in env["hints"]["next_actions"][0]
    code, env, _ = cli("resume", "add", str(RESUMES / "senior-backend.docx"))
    draft = yaml.safe_load(paths.profile_draft.read_text(encoding="utf-8"))
    assert draft["headline"] == "Reviewed headline"  # the first draft's content stays
    assert [(r["file"], r["default"]) for r in draft["resumes"]] == [
        ("newgrad-analyst.pdf", True),
        ("intl-sponsorship.pdf", False),
        ("senior-backend.docx", False),
    ]
    assert draft["resumes"][0]["label"] == "analytics"
    code, env, _ = cli("resume", "add", str(RESUMES / "intl-sponsorship.pdf"), "--redraft")
    draft = yaml.safe_load(paths.profile_draft.read_text(encoding="utf-8"))
    assert draft["headline"] != "Reviewed headline"  # drafted from this resume
    assert len(draft["resumes"]) == 3  # the list survives a redraft


def test_no_default_resume_is_allowed(cli, isolated_home):
    cli("resume", "add", str(RESUMES / "newgrad-analyst.pdf"), "--not-default")
    cli("resume", "add", str(RESUMES / "intl-sponsorship.pdf"), "--not-default")
    paths = get_paths()
    draft = yaml.safe_load(paths.profile_draft.read_text(encoding="utf-8"))
    assert not any(r["default"] for r in draft["resumes"])
    code, env, _ = cli("resume", "add", str(RESUMES / "senior-backend.pdf"), "--not-default")
    assert code == ExitCode.OK
    assert any("No default resume" in a for a in env["hints"]["next_actions"])


def test_profile_set_parses_json_objects_and_arrays():
    from jobhunt.onboarding.profile import parse_value

    v = parse_value('[{"name": "Bike Demand", "summary": "Forecasts rides", "tech": ["Python"]}]')
    assert v == [{"name": "Bike Demand", "summary": "Forecasts rides", "tech": ["Python"]}]
    assert parse_value('{"school": "Riverside University", "majors": ["Math"]}') == {
        "school": "Riverside University",
        "majors": ["Math"],
    }
    assert parse_value("[a, b]") == ["a", "b"]  # flow lists still work
    assert parse_value("[2027, 2028]") == ["2027", "2028"]  # scalars stay text
    assert parse_value("2027-05") == "2027-05" and parse_value("null") is None


def test_profile_set_with_an_object_reaches_the_profile(cli, persona_home):
    home = persona_home("newgrad-analyst")
    code, env, _ = cli(
        "profile", "set", "projects",
        '[{"name": "Bike Demand", "summary": "Forecasts daily rides", "tech": ["Python"]}]',
    )  # fmt: skip
    assert code == ExitCode.OK, env
    raw = (home / "profile.yaml").read_text(encoding="utf-8")
    prof = Profile.model_validate(yaml.safe_load(raw))
    assert prof.projects[0].name == "Bike Demand" and prof.projects[0].tech == ["Python"]


def test_resume_add_dry_run_writes_nothing(cli, isolated_home):
    code, _env, _ = cli("resume", "add", str(RESUMES / "first-year-intern.docx"), "--dry-run")
    assert code == ExitCode.OK
    assert not isolated_home.exists() or not any(isolated_home.rglob("*.docx"))


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
    prefs = yaml.safe_load(
        (PERSONAS_DIR / persona / "preferences.yaml").read_text(encoding="utf-8")
    )
    answers = yaml.safe_load((PERSONAS_DIR / persona / "answers.yaml").read_text(encoding="utf-8"))
    vals = F.prefill(prefs, answers, profile=None, contact=None)
    html = F.render(vals, token="t", new_questions=[])
    assert "<script>" in html and 'name="token" value="t"' in html
    # every labelled control is linked to its label (screen readers, click-to-focus)
    import re

    for target in re.findall(r'<label class="lbl" for="([^"]+)"', html):
        assert f'id="{target}"' in html
    labels = re.findall(r'<label class="lbl" for="[^"]+">([^<]+)</label>', html)
    assert len(labels) == len(set(labels)), "every label is distinct (one per country)"
    p2, a2, errors = F.parse(
        _post_from_values(vals), existing_prefs=prefs, existing_answers=answers, new_questions=[]
    )
    assert not errors and not F.validate(p2, a2)
    assert Preferences.model_validate(p2) == Preferences.model_validate(prefs)
    assert Answers.model_validate(a2) == Answers.model_validate(answers)


def test_form_prefills_from_resume_and_profile():
    prof = Profile.model_validate(
        yaml.safe_load(
            (PERSONAS_DIR / "newgrad-analyst" / "profile.yaml").read_text(encoding="utf-8")
        )
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
    prefs = yaml.safe_load((home / "preferences.yaml").read_text(encoding="utf-8"))
    answers = yaml.safe_load((home / "answers.yaml").read_text(encoding="utf-8"))
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
    code, page, _ = _req(f"{base}/save", urllib.parse.urlencode({"token": "nope"}).encode())
    assert code == 403 and "Nothing was saved" in page  # a tab from an older session
    code, _, hdrs = _req(
        f"{base}/save", body, {"Origin": base, "Content-Type": "application/x-www-form-urlencoded"}
    )
    assert code == 303 and "saved=" in hdrs["Location"]
    code, page, _ = _req(base + hdrs["Location"])
    assert code == 200 and "<b>Saved.</b>" in page and "Last saved 20" in page
    code, page, _ = _req(base + hdrs["Location"])  # a reload (or an old tab on that URL)
    assert code == 200 and "<b>Saved.</b>" not in page and "Last saved 20" in page
    assert "<b>Saved.</b>" not in _req(f"{base}/?t={token}&saved=1")[1]
    saved = Answers.model_validate(
        yaml.safe_load((home / "answers.yaml").read_text(encoding="utf-8"))
    )
    assert saved.standard.sms_consent == "no"
    assert saved.identity.email == answers["identity"]["email"]
    st = server.read_state(paths)
    assert st["status"] == "saved" and st["saves"] == 1
    # the open page checks its link: a stale one disables Save
    assert _req(f"{base}/ping?t={token}")[0] == 200
    assert _req(f"{base}/ping?t=old-token")[0] == 403
    assert "connect-src 'self'" in _req(f"{base}/?t={token}")[2]["Content-Security-Policy"]
    # invalid input: not saved, the form comes back with the error
    bad = urllib.parse.urlencode({"token": token, "p:timing.earliest_start": "soon"}).encode()
    code, page, _ = _req(f"{base}/save", bad, {"Origin": base})
    assert code == 400 and "Not saved yet" in page


def test_setup_form_launch_status_stop(cli, persona_home):
    persona_home("newgrad-analyst")
    code, env, _ = cli("setup", "form")
    try:
        assert code == ExitCode.OK, env
        d = env["data"]
        assert d["running"] and d["url"].startswith("http://127.0.0.1:")
        assert env["hints"]["user_actions"]
        code, _page, _ = _req(d["url"])
        assert code == 200
        # a second start reuses the running server
        _, env2, _ = cli("setup", "form")
        assert env2["data"]["url"] == d["url"] and env2["data"]["reused"]
        assert "already running at " + d["url"] in env2["hints"]["user_actions"][0]
        _, st, _ = cli("setup", "form", "--status")
        assert st["data"]["status"] == "waiting"
    finally:
        _, stopped, _ = cli("setup", "form", "--stop")
    assert stopped["data"]["running"] is False
    assert stopped["data"]["status"] in ("stopped", "saved_and_stopped")


def test_form_server_binds_without_reverse_dns(monkeypatch, isolated_home):
    """A slow reverse DNS lookup once stalled startup for 20s+ on macOS CI."""
    import socket

    from jobhunt.onboarding import server as srv
    from jobhunt.paths import get_paths

    def no_dns(*_a, **_k):
        raise AssertionError("server_bind must not do a reverse DNS lookup")

    monkeypatch.setattr(socket, "getfqdn", no_dns)
    httpd = srv.make_server(get_paths(), "tok", 0)
    try:
        assert httpd.server_name == "127.0.0.1" and httpd.server_port > 0
    finally:
        httpd.server_close()
