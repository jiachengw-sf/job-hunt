"""Essays: grounded drafts, one review pass applied by code, the user's approval, pre-fill."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from jobhunt.drafts.grounding import edit_ratio, leadership, numbers
from jobhunt.errors import ExitCode
from tests.conftest import REPO

BASE = REPO / "tests" / "fixtures" / "sources" / "base"
OVERLAY = REPO / "tests" / "fixtures" / "forms" / "overlay"


@pytest.fixture(scope="module")
def fx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("fx")
    shutil.copytree(BASE, d, dirs_exist_ok=True)
    shutil.copytree(OVERLAY, d, dirs_exist_ok=True)
    return d


def _db(home: Path) -> sqlite3.Connection:
    c = sqlite3.connect(home / "jobhunt.db")
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture
def setup(cli, persona_home, fx):
    def _go(config: str = "") -> tuple[Path, int]:
        home = persona_home("intl-sponsorship")
        if config:
            (home / "config.yaml").write_text(config)
        assert cli("run", "--fixtures", str(fx), "--no-sync", "--no-inspect")[0] == 0
        with _db(home) as c:
            ids = [str(r[0]) for r in c.execute("SELECT id FROM jobs WHERE live=1")]
        assert cli("inspect", *ids, "--fixtures", str(fx), "--no-match")[0] == 0
        with _db(home) as c:
            jid = c.execute("SELECT job_id FROM form_specs WHERE class='essay'").fetchone()[0]
        return home, jid

    return _go


def _task(cli, jid: int) -> dict:
    code, env, _ = cli("essays", "draft", str(jid))
    assert code == ExitCode.OK, env
    return json.loads(Path(env["data"]["task_path"]).read_text(encoding="utf-8"))


def _posting_quote(task: dict) -> str:
    body = [ln for ln in task["posting"].splitlines() if ln and not ln.startswith("<")]
    return max(body, key=len)[:80].strip()


def _line(task: dict, want_digit: bool = False) -> dict:
    lines = [s for s in task["sources"] if s["ref"].startswith(("experience", "projects"))]
    if want_digit:
        lines = [s for s in lines if any(ch.isdigit() for ch in s["text"])]
    return lines[0]


def _good(task: dict) -> dict:
    line = _line(task)
    q = _posting_quote(task)
    return {
        "task_id": task["task_id"],
        "job_id": task["job"]["id"],
        "essays": [
            {
                "field_id": task["prompts"][0]["field_id"],
                "sentences": [
                    {"text": f"Your posting describes this: {q}.", "source": "posting", "quote": q},
                    {"text": line["text"] + ".", "source": line["ref"]},
                    {"text": "That is why this role stands out to me.", "source": None},
                ],
            }
        ],
    }


def _ingest(cli, task: dict, res: dict) -> tuple[int, dict]:
    Path(task["result_path"]).write_text(json.dumps(res), encoding="utf-8")
    code, env, _ = cli("ingest")
    return code, env


def test_claim_detectors():
    assert numbers("Cut latency from 900 ms to 1,200") == {"900", "1200"}
    assert leadership("I led a team and mentored two") == {"lead", "mentor"}
    assert leadership("I lead-in to the topic") == set()
    assert edit_ratio("same text", "same  text") == 0.0
    assert edit_ratio("abc", "xyz") == 1.0


def test_draft_task_has_prompts_sources_and_no_personal_data(cli, setup):
    home, jid = setup()
    t = _task(cli, jid)
    assert t["prompts"][0]["required"] and t["posting"].startswith("<untrusted_content")
    refs = {s["ref"] for s in t["sources"]}
    assert "headline" in refs and t["style_skill"] is None
    text = json.dumps(t)
    answers = (home / "answers.yaml").read_text()
    for needle in ("@", "555-01"):
        if needle in answers:
            assert needle not in text.replace("@context", "")
    _, again, _ = cli("essays", "draft", str(jid))
    assert again["data"]["status"] == "pending"


def test_style_skill_is_named_in_the_task(cli, setup):
    _, jid = setup("writing_style_skill: my-voice\n")
    t = _task(cli, jid)
    assert t["style_skill"] == "my-voice" and "'my-voice'" in t["instructions"]


def test_good_draft_is_saved_as_a_draft_to_edit(cli, setup):
    _home, jid = setup()
    t = _task(cli, jid)
    code, env = _ingest(cli, t, _good(t))
    assert code == ExitCode.OK, env
    [f] = env["data"]["accepted"][0]["summary"]["files"]
    body = Path(f).read_text(encoding="utf-8")
    assert body.startswith("# DRAFT — edit before use") and "That is why" in body
    _, shown, _ = cli("essays", "show", str(jid))
    d = shown["data"]["drafts"][0]
    assert d["label"] == "draft — edit before use" and d["status"] == "draft"
    assert shown["hints"]["user_actions"]
    _, lst, _ = cli("essays", "list")
    assert lst["data"]["jobs"][0]["fields"][0]["status"] == "draft"


@pytest.mark.parametrize(
    ("mutate", "words"),
    [
        (
            lambda t, r: r["essays"][0]["sentences"][1].update(
                text=_line(t)["text"] + " for 4000 users"
            ),
            "number(s) 4000",
        ),
        (
            lambda t, r: r["essays"][0]["sentences"][1].update(
                text=_line(t)["text"] + " on Kubernetes"
            ),
            "skill(s) Kubernetes",
        ),
        (
            lambda t, r: r["essays"][0]["sentences"][1].update(text="I led the team that did it."),
            "leadership claim",
        ),
        (
            lambda t, r: r["essays"][0]["sentences"][2].update(text="I am fluent in Kubernetes."),
            "cite the line",
        ),
        (
            lambda t, r: r["essays"][0]["sentences"][1].update(source="experience[42]"),
            "not a source ref",
        ),
        (
            lambda t, r: r["essays"][0]["sentences"][0].update(quote="we pay double"),
            "quote the posting exactly",
        ),
        (lambda t, r: r["essays"][0].update(field_id="question_nope"), "not a prompt here"),
        (lambda t, r: r.update(essays=[]), "draft the required prompt"),
    ],
)
def test_ungrounded_drafts_are_rejected(cli, setup, mutate, words):
    _, jid = setup()
    t = _task(cli, jid)
    res = _good(t)
    mutate(t, res)
    code, env = _ingest(cli, t, res)
    assert code == ExitCode.VALIDATION
    msgs = [e["message"] for e in env["data"]["rejected"][0]["errors"]]
    assert any(words in m for m in msgs), msgs


def test_character_limit_is_enforced(cli, setup):
    home, jid = setup()
    with _db(home) as c:
        d = json.loads(c.execute("SELECT details_json FROM form_specs WHERE job_id=?",
                                 (jid,)).fetchone()[0])  # fmt: skip
        for f in d["fields"]:
            if f["resolution"] == "essay":
                f["max_length"] = 60
        c.execute("UPDATE form_specs SET details_json=? WHERE job_id=?", (json.dumps(d), jid))
    t = _task(cli, jid)
    assert t["prompts"][0]["max_length"] == 60
    code, env = _ingest(cli, t, _good(t))
    assert code == ExitCode.VALIDATION
    assert "the form allows 60" in env["data"]["rejected"][0]["errors"][0]["message"]


def test_hidden_unicode_is_stripped(cli, setup):
    _home, jid = setup()
    t = _task(cli, jid)
    res = _good(t)
    res["essays"][0]["sentences"][2]["text"] = "That is why​ this role‮ stands out."
    assert _ingest(cli, t, res)[0] == ExitCode.OK
    _, shown, _ = cli("essays", "show", str(jid))
    assert "That is why this role stands out." in shown["data"]["drafts"][0]["text"]


def _drafted(cli, setup):
    home, jid = setup()
    t = _task(cli, jid)
    assert _ingest(cli, t, _good(t))[0] == ExitCode.OK
    code, env, _ = cli("essays", "review", str(jid))
    assert code == ExitCode.OK and env["data"]["status"] == "written"
    rt = json.loads(Path(env["data"]["task_path"]).read_text(encoding="utf-8"))
    assert rt["fresh_context"] and rt["drafts"][0]["sentences"]
    return home, jid, rt


def _review(rt: dict, edits: list[dict]) -> dict:
    return {"task_id": rt["task_id"], "job_id": rt["job"]["id"], "edits": edits}


def test_review_edits_are_applied_by_code_once(cli, setup):
    _home, jid, rt = _drafted(cli, setup)
    fid = rt["drafts"][0]["field_id"]
    edit = {"field_id": fid, "old": "stands out to me", "new": "fits what I want to learn",
            "reason": "more specific"}  # fmt: skip
    code, env = _ingest(cli, rt, _review(rt, [edit]))
    assert code == ExitCode.OK, env
    _, shown, _ = cli("essays", "show", str(jid))
    d = shown["data"]["drafts"][0]
    assert d["status"] == "reviewed" and "fits what I want to learn" in d["text"]
    assert d["review_edits"][0]["reason"] == "more specific"
    assert "fits what I want to learn" in Path(d["file"]).read_text(encoding="utf-8")
    _, again, _ = cli("essays", "review", str(jid))
    assert again["data"]["status"] == "reviewed" and again["data"]["task_id"] is None


@pytest.mark.parametrize(
    ("edit", "words"),
    [
        ({"old": "no such words", "new": "x"}, "not found in the draft"),
        ({"old": "t", "new": "x"}, "appears"),
        ({"old": "stands out to me", "new": "stands out after 12 years"}, "number(s) 12"),
        ({"old": "stands out to me", "new": "stands out since I led teams"}, "leadership"),
    ],
)
def test_bad_review_edits_are_rejected(cli, setup, edit, words):
    _, _, rt = _drafted(cli, setup)
    e = {"field_id": rt["drafts"][0]["field_id"], "reason": "because", **edit}
    code, env = _ingest(cli, rt, _review(rt, [e]))
    assert code == ExitCode.VALIDATION
    assert any(words in x["message"] for x in env["data"]["rejected"][0]["errors"])


def test_review_never_overwrites_your_edits(cli, setup):
    _home, jid, rt = _drafted(cli, setup)
    _, shown, _ = cli("essays", "show", str(jid))
    f = Path(shown["data"]["drafts"][0]["file"])
    f.write_text(f.read_text(encoding="utf-8") + "\nMy own closing line.\n", encoding="utf-8")
    edit = {"field_id": rt["drafts"][0]["field_id"], "old": "stands out to me",
            "new": "matters to me", "reason": "tone"}  # fmt: skip
    code, env = _ingest(cli, rt, _review(rt, [edit]))
    assert code == ExitCode.OK
    [alt] = env["data"]["accepted"][0]["summary"]["kept_your_edits"]
    assert "matters to me" in Path(alt).read_text(encoding="utf-8")
    assert "My own closing line." in f.read_text(encoding="utf-8")


def test_approve_records_edits_and_nudges_when_unchanged(cli, setup):
    _home, jid, _ = _drafted(cli, setup)
    code, env, _ = cli("essays", "approve", str(jid))
    assert code == ExitCode.OK
    a = env["data"]["approved"][0]
    assert a["unchanged"] is True and a["edit_ratio"] < 0.05
    assert "almost exactly as drafted" in env["hints"]["user_actions"][0]
    _, shown, _ = cli("essays", "show", str(jid))
    f = Path(shown["data"]["drafts"][0]["file"])
    f.write_text("# my notes\nI have wanted to build robots since I was a kid.​\n", encoding="utf-8")
    code, env, _ = cli("essays", "approve", str(jid))
    a = env["data"]["approved"][0]
    assert a["unchanged"] is False and a["edit_ratio"] > 0.5
    assert env["hints"]["user_actions"] == []
    _, shown, _ = cli("essays", "show", str(jid))
    assert shown["data"]["drafts"][0]["text"] == "I have wanted to build robots since I was a kid."


def test_approve_enforces_the_limit(cli, setup):
    home, jid, _ = _drafted(cli, setup)
    with _db(home) as c:
        c.execute("UPDATE drafts SET max_length=20")
    code, env, _ = cli("essays", "approve", str(jid))
    assert code == ExitCode.VALIDATION and "allows 20" in env["error"]["message"]


def test_approved_essay_reaches_answers_json_and_packet(cli, setup):
    _home, jid, _ = _drafted(cli, setup)
    code, env, _ = cli("prefill", str(jid), "--dry-run")
    j = env["data"]["jobs"][0]
    assert j["status"] == "skipped" and "essays approve" in j["reason"]
    _, shown, _ = cli("essays", "show", str(jid))
    f = Path(shown["data"]["drafts"][0]["file"])
    f.write_text(
        "Robots that sort parcels are the kind of thing I want to build.\n", encoding="utf-8"
    )
    cli("essays", "approve", str(jid))
    code, env, _ = cli("packet", str(jid))
    assert code == ExitCode.OK
    page = Path(env["data"]["path"]).read_text(encoding="utf-8")
    assert "Robots that sort parcels" in page
    code, env, _ = cli("prefill", str(jid), "--dry-run")
    j = env["data"]["jobs"][0]
    assert j["status"] == "queued", j
    assert "Robots that sort" not in json.dumps(env)  # values never go into an envelope


def test_answers_json_fills_the_approved_text(cli, setup):
    from jobhunt.cli.apply import form_inputs
    from jobhunt.cli.prefill import prepare_job
    from jobhunt.config import load_config
    from jobhunt.paths import get_paths
    from jobhunt.prefill.values import AnswerSheet

    home, jid, _ = _drafted(cli, setup)
    _, shown, _ = cli("essays", "show", str(jid))
    Path(shown["data"]["drafts"][0]["file"]).write_text(
        "Parcel robots are my thing.\n", encoding="utf-8"
    )
    cli("essays", "approve", str(jid))
    paths = get_paths()
    inp = form_inputs(paths, load_config(paths), "2026-10-18T00:00:00Z")
    with _db(home) as c:
        job, _ = prepare_job(c, jid, inp, paths, "2026-10-18T00:00:00Z", dry_run=False)
    assert job.status == "queued"
    sheet = AnswerSheet.model_validate_json((paths.prefill / str(jid) / "answers.json").read_text())
    essay = next(f for f in sheet.fields if f.value == "Parcel robots are my thing.")
    assert essay.action == "fill"
