"""`jobhunt calibrate`: band labels from the user's own tracker, suggestions, never tuning."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
import yaml

from jobhunt.config import deep_merge, default_config_data
from jobhunt.errors import ExitCode
from jobhunt.fit.calibrate import calibrate
from jobhunt.models import Config
from jobhunt.store.db import migrate
from jobhunt.tracker.store import mark
from tests.conftest import PERSONAS, REPO

NOW = "2026-10-18T12:00:00Z"
TODAY = date(2026, 10, 18)
BASE = str(REPO / "tests" / "fixtures" / "sources" / "base")

# Status paths a job walks through, as the user would mark them.
NO_REPLY = ["Applied"]
REJECTED = ["Applied", "Rejected"]
INTERVIEW = ["Applied", "Interviewing"]
INTERVIEW_THEN_REJECTED = ["Applied", "OA", "Interviewing", "Rejected"]
OFFER = ["Applied", "Interviewing", "Offer"]


def cfg(**overrides) -> Config:
    return Config.model_validate(deep_merge(default_config_data(), overrides))


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    migrate(c)
    yield c
    c.close()


def add(conn, band: str | None, path: list[str], n: int = 1, skip: str | None = None) -> list[int]:
    ids = []
    for _ in range(n):
        i = conn.execute("SELECT coalesce(max(id), 0) + 1 FROM jobs").fetchone()[0]
        conn.execute(
            "INSERT INTO jobs (id, dedupe_key, company, title, first_seen, last_seen) "
            "VALUES (?, ?, 'Acme', 'Role', ?, ?)",
            (i, f"k{i}", NOW, NOW),
        )
        conn.execute(
            "INSERT INTO fits (job_id, band, provisional, computed_at) VALUES (?, ?, 0, ?)",
            (i, band, NOW),
        )
        for status in path:
            mark(conn, i, status, reason=None, applied_on=None, today=TODAY, now=NOW)
        if skip:
            mark(conn, i, "Skipped", reason=skip, applied_on=None, today=TODAY, now=NOW)
        ids.append(i)
    return ids


def band(report, name):
    return next(b for b in report.bands if b.band == name)


def test_separating_bands_need_no_change(conn):
    add(conn, "strong", INTERVIEW, 4)
    add(conn, "strong", REJECTED, 1)
    add(conn, "good", INTERVIEW, 3)
    add(conn, "good", REJECTED, 3)
    add(conn, "stretch", INTERVIEW, 1)
    add(conn, "stretch", REJECTED, 5)
    r = calibrate(conn, cfg())
    assert [band(r, b).label for b in ("strong", "good", "stretch")] == ["separating"] * 3
    assert band(r, "strong").compared_with == "good"
    assert band(r, "stretch").compared_with == "good"  # the lowest band compares upward
    assert band(r, "not_a_fit").label == "insufficient_data"
    assert band(r, "strong").interview_rate == 0.8
    assert not [s for s in r.suggestions if s.kind == "config"]
    assert r.config_snippet is None


def test_inverted_bands_suggest_a_threshold_change_and_never_apply_it(conn):
    add(conn, "strong", INTERVIEW, 1)
    add(conn, "strong", REJECTED, 4)
    add(conn, "good", INTERVIEW, 4)
    add(conn, "good", REJECTED, 1)
    c = cfg()
    r = calibrate(conn, c)
    assert band(r, "strong").label == "inverted"
    assert band(r, "good").label == "inverted" and band(r, "good").compared_with == "strong"
    s = next(s for s in r.suggestions if s.key == "fit.bands.strong")
    assert (s.current, s.suggested) == (0.8, 0.85)
    assert "20%" in s.reason and "80%" in s.reason
    snippet = yaml.safe_load(r.config_snippet)
    assert snippet == {"fit": {"bands": {"strong": 0.85}}}
    cfg(**snippet)  # the snippet is valid config as written
    assert c.fit.bands.strong == 0.8  # nothing tuned itself


def test_flat_bands(conn):
    add(conn, "good", INTERVIEW, 2)
    add(conn, "good", REJECTED, 3)
    add(conn, "stretch", INTERVIEW, 2)
    add(conn, "stretch", REJECTED, 3)
    r = calibrate(conn, cfg())
    assert band(r, "good").label == band(r, "stretch").label == "flat"
    assert any(s.key == "fit.bands.good" for s in r.suggestions)


def test_too_few_replies_is_insufficient_data(conn):
    add(conn, "strong", INTERVIEW, 4)
    add(conn, "good", REJECTED, 4)
    r = calibrate(conn, cfg())
    assert {b.label for b in r.bands} == {"insufficient_data"}
    assert not r.suggestions
    # the threshold is config, not code
    r = calibrate(conn, cfg(calibration={"min_responses": 4}))
    assert band(r, "strong").label == "separating"


def test_no_reply_is_never_evidence_of_poor_fit(conn):
    add(conn, "strong", INTERVIEW, 5)
    add(conn, "good", INTERVIEW, 2)
    add(conn, "good", REJECTED, 3)
    before = calibrate(conn, cfg())
    add(conn, "strong", NO_REPLY, 40)  # silence from 40 strong applications
    add(conn, "strong", ["Applied", "Withdrawn"], 3)  # withdrawn before any reply
    after = calibrate(conn, cfg())
    s = band(after, "strong")
    assert s.applied == 48 and s.no_reply == 43 and s.replies == 5
    assert s.interview_rate == band(before, "strong").interview_rate == 1.0
    assert [b.label for b in after.bands] == [b.label for b in before.bands]
    assert after.suggestions == before.suggestions


def test_status_history_counts_interviews_that_ended_in_rejection(conn):
    add(conn, "good", INTERVIEW_THEN_REJECTED, 2)
    add(conn, "good", OFFER, 1)
    add(conn, "good", REJECTED, 1)
    s = band(calibrate(conn, cfg()), "good")
    assert (s.interviews, s.offers, s.rejected, s.replies) == (3, 1, 1, 4)
    assert s.offer_rate == 0.25


def test_unbanded_jobs_are_reported_but_not_labelled(conn):
    add(conn, None, INTERVIEW, 2)
    r = calibrate(conn, cfg())
    u = band(r, "unbanded")
    assert u.interviews == 2 and u.label is None


def test_skip_reasons_feed_suggestions(conn):
    add(conn, "good", [], 3, skip="too_senior")
    add(conn, "strong", [], 1, skip="too_senior")
    add(conn, "strong", [], 3, skip="pay")
    add(conn, "good", [], 1, skip="other")
    add(conn, "stretch", [], 5, skip="location")  # low bands don't drive suggestions
    r = calibrate(conn, cfg())
    assert r.skip_reasons["too_senior"] == {"good": 3, "strong": 1}
    assert r.skip_reasons["location"] == {"stretch": 5}
    keys = {s.key: s for s in r.suggestions if s.kind == "config"}
    assert (
        keys["fit.level_mismatch_years"].current,
        keys["fit.level_mismatch_years"].suggested,
    ) == (
        3,
        2,
    )
    assert keys["fit.preference.pay"].suggested == 0.35
    assert "pay floor" in keys["fit.preference.pay"].reason
    assert "fit.preference.location" not in keys
    assert r.applied == 0  # skips are not applications
    cfg(**yaml.safe_load(r.config_snippet))


def test_rubric_suggestion_from_how_lower_band_interviews_scored(conn):
    add(conn, "strong", INTERVIEW, 1)
    add(conn, "strong", REJECTED, 4)
    good = add(conn, "good", INTERVIEW, 4)
    add(conn, "good", REJECTED, 1)
    for j in good:
        for k in range(3):
            conn.execute(
                "INSERT INTO requirements (job_id, text, importance, type, status, tier) "
                "VALUES (?, ?, 'must', 'skill', 'met', ?)",
                (j, f"req {k}", "inferred" if k else "stated"),
            )
    r = calibrate(conn, cfg())
    s = next(s for s in r.suggestions if s.key == "fit.qualification.met_inferred")
    assert (s.current, s.suggested) == (0.6, 0.65) and sorted(s.job_ids) == good


@pytest.mark.parametrize("persona", PERSONAS)
def test_calibrate_on_persona_tracker(cli, persona_home, persona):
    home = persona_home(persona)
    cli("run", "--fixtures", BASE, "--no-sync")
    code, env, _ = cli("jobs", "--limit", "200")
    ids = [j["id"] for j in env["data"]["jobs"]]
    assert len(ids) >= 2
    # Stand in for ingested judgments: give the persona's jobs bands.
    db = sqlite3.connect(home / "jobhunt.db")
    for n, i in enumerate(ids):
        db.execute("UPDATE fits SET band=? WHERE job_id=?", ("good" if n % 2 else "strong", i))
    db.commit()
    db.close()
    cli("mark", str(ids[0]), "Applied")
    cli("mark", str(ids[0]), "Interviewing")
    cli("mark", str(ids[1]), "Applied")
    cli("mark", str(ids[1]), "Rejected")
    if len(ids) > 2:
        cli("mark", str(ids[2]), "Applied")
    before = (home / "jobhunt.db").read_bytes()
    code, env, _ = cli("calibrate")
    assert code == ExitCode.OK, env
    d = env["data"]
    assert d["applied"] == min(3, len(ids)) and d["replies"] == 2
    assert d["no_reply"] == d["applied"] - 2
    assert band_named(d, "strong")["interviews"] == 1 and band_named(d, "good")["rejected"] == 1
    assert {b["label"] for b in d["bands"]} == {"insufficient_data"}
    assert (home / "jobhunt.db").read_bytes() == before  # read-only
    assert not (home / "config.yaml").exists()


def band_named(d, name):
    return next(b for b in d["bands"] if b["band"] == name)


def test_calibrate_with_an_empty_tracker_says_what_to_do(cli, persona_home):
    persona_home("senior-backend")
    code, env, _ = cli("calibrate")
    assert code == ExitCode.OK
    assert env["data"]["applied"] == 0
    assert any("jobhunt mark" in a for a in env["hints"]["next_actions"])
