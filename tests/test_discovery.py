"""Discovery end to end, offline: `jobhunt fetch` and `jobhunt add` against the fixture world."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from jobhunt.errors import ExitCode
from tests.conftest import PERSONAS

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sources"
BASE = FIXTURES / "base"
UPDATE = FIXTURES / "update"
BF_DS = "4d1c2e7a-0b6f-4c1e-9a7d-2f3b5c6d7e01"


@pytest.fixture
def world(tmp_path: Path):
    """A writable copy of the fixture world; `world.update()` applies the later snapshot."""
    root = tmp_path / "world"
    shutil.copytree(BASE, root)

    class World:
        path = root

        def update(self) -> None:
            shutil.copytree(UPDATE, root, dirs_exist_ok=True)

        def reset(self) -> None:
            shutil.rmtree(root)
            shutil.copytree(BASE, root)

    return World()


def db(home: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(home / "jobhunt.db")
    conn.row_factory = sqlite3.Row
    return conn


def counts(home: Path) -> dict[str, int]:
    c = db(home)
    try:
        return {
            t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("jobs", "job_urls", "events", "source_jobs", "signals")
        }
    finally:
        c.close()


def fetch(cli, world, *extra):
    code, env, _ = cli("fetch", "--fixtures", str(world.path), *extra)
    assert code == 0, env
    return env["data"]


# --- every persona ----------------------------------------------------------------------------


EXPECTED_SOURCES = {
    "first-year-intern": {"lever:bluefin", "ashby:quillstack", "simplify:Summer2027-Internships"},
    "intl-sponsorship": {
        "greenhouse:northwind",
        "ashby:quillstack",
        "simplify:Summer2027-Internships",
    },
    "newgrad-analyst": {"greenhouse:ledgerline", "simplify:New-Grad-Positions"},
    "senior-backend": {"greenhouse:tessellate", "lever:driftwood", "ashby:kitewire"},
}


@pytest.mark.parametrize("persona", PERSONAS)
def test_fetch_offline_for_every_persona(cli, persona_home, world, persona):
    home = persona_home(persona)
    data = fetch(cli, world)
    assert {s["source_id"] for s in data["sources"]} == EXPECTED_SOURCES[persona]
    assert all(s["status"] == "ok" and s["baseline"] for s in data["sources"])
    assert data["totals"]["new"] > 0 and data["totals"]["errors"] == 0
    assert data["new_jobs"] == []  # silent first run: baseline only
    first = counts(home)

    # Re-run: 304s, nothing new, no duplicates, no new events.
    again = fetch(cli, world)
    assert all(s["status"] == "not_modified" for s in again["sources"])
    assert again["totals"]["new"] == 0
    assert counts(home) == first

    # Re-run with full bodies (ETags forgotten): still no duplicates or events.
    c = db(home)
    c.execute("UPDATE sources SET etag=NULL, last_modified=NULL")
    c.commit()
    c.close()
    full = fetch(cli, world)
    assert all(s["status"] == "ok" and not s["baseline"] for s in full["sources"])
    assert full["totals"] == {**full["totals"], "new": 0, "changed": 0, "closed": 0, "reopened": 0}
    assert counts(home) == first

    # Yields are logged for every source on every run.
    c = db(home)
    rows = c.execute("SELECT run_id, source_id, fetched, new FROM source_yields").fetchall()
    c.close()
    assert len(rows) == 3 * len(EXPECTED_SOURCES[persona])


def test_senior_backend_has_no_simplify_and_warns_about_custom_page(cli, persona_home, world):
    persona_home("senior-backend")
    data = fetch(cli, world)
    assert not any(s["kind"] == "simplify" for s in data["sources"])
    assert any("Brightpath Health" in w for w in data["warnings"])


def test_unsupported_watchlist_ats_is_recorded_by_name(cli, persona_home, world):
    persona_home("newgrad-analyst")
    data = fetch(cli, world)
    assert {"company": "Saltmarsh Media", "ats": "smartrecruiters"} in data["unsupported_watchlist"]
    assert data["coverage"]["smartrecruiters"]["supported"] is False
    assert data["coverage"]["workday"]["live_jobs"] == 1


# --- dedupe and merge -------------------------------------------------------------------------


def test_cross_source_merge_keeps_both_urls_and_prefers_ats_data(cli, persona_home, world):
    home = persona_home("intl-sponsorship")
    fetch(cli, world)
    c = db(home)
    job = c.execute("SELECT * FROM jobs WHERE dedupe_key='greenhouse:northwind:1001'").fetchone()
    urls = {r[0] for r in c.execute("SELECT url FROM job_urls WHERE job_id=?", (job["id"],))}
    sources = {
        r[0] for r in c.execute("SELECT source_id FROM source_jobs WHERE job_id=?", (job["id"],))
    }
    n = c.execute(
        "SELECT count(*) FROM jobs WHERE title LIKE 'Software Engineering Intern%'"
    ).fetchone()[0]
    c.close()
    assert n == 1
    assert sources == {"greenhouse:northwind", "simplify:Summer2027-Internships"}
    assert "https://job-boards.greenhouse.io/northwind/jobs/1001?gh_src=simplify" in urls
    assert "https://job-boards.greenhouse.io/northwind/jobs/1001" in urls
    assert job["posted_at"] == "2026-10-01T14:00:00Z"  # Greenhouse first_published wins
    assert job["description"] and json.loads(job["pay_json"])["min"] == 42


def test_same_title_in_two_locations_is_two_jobs_not_a_duplicate(cli, persona_home, world):
    home = persona_home("senior-backend")
    fetch(cli, world)
    c = db(home)
    rows = c.execute(
        "SELECT locations_json FROM jobs WHERE title='Senior Backend Engineer'"
    ).fetchall()
    c.close()
    assert sorted(json.loads(r[0])[0] for r in rows) == ["Austin, TX", "Remote - US"]


def test_aggregator_duplicate_rows_collapse_and_union_locations(cli, persona_home, world):
    home = persona_home("first-year-intern")
    fetch(cli, world)
    c = db(home)
    rows = c.execute(
        "SELECT locations_json FROM jobs WHERE title LIKE 'Technology Intern%'"
    ).fetchall()
    c.close()
    assert len(rows) == 1
    assert json.loads(rows[0][0]) == ["Charlotte, NC", "Wilmington, DE"]


def test_posted_at_is_never_inferred(cli, persona_home, world):
    home = persona_home("intl-sponsorship")
    fetch(cli, world)
    c = db(home)
    gh_undated = c.execute(
        "SELECT posted_at, first_seen FROM jobs WHERE dedupe_key='greenhouse:northwind:1004'"
    ).fetchone()
    c.close()
    assert gh_undated["posted_at"] is None and gh_undated["first_seen"]


def test_ashby_published_at_is_not_a_posting_date(cli, persona_home, world):
    home = persona_home("senior-backend")
    fetch(cli, world)
    c = db(home)
    row = c.execute("SELECT posted_at FROM jobs WHERE ats='ashby'").fetchone()
    c.close()
    assert row["posted_at"] is None  # publishedAt is the last publish, not the first


def test_unlisted_and_inactive_rows_are_skipped(cli, persona_home, world):
    home = persona_home("first-year-intern")
    fetch(cli, world)
    c = db(home)
    titles = {r[0] for r in c.execute("SELECT company FROM jobs")}
    hidden = c.execute(
        "SELECT count(*) FROM jobs WHERE title='Backend Engineer Intern'"
    ).fetchone()[0]
    c.close()
    assert "Old Closed Co" not in titles and "Hidden Co" not in titles
    assert hidden == 0  # Ashby isListed=false


# --- events, closures, signals ----------------------------------------------------------------


def events(home: Path, kind: str) -> list[sqlite3.Row]:
    c = db(home)
    rows = c.execute(
        "SELECT e.*, j.dedupe_key FROM events e JOIN jobs j ON j.id=e.job_id WHERE kind=?", (kind,)
    ).fetchall()
    c.close()
    return rows


def test_baseline_events_are_marked_and_later_ones_alert(cli, persona_home, world):
    home = persona_home("intl-sponsorship")
    fetch(cli, world)
    appeared = events(home, "appeared")
    assert appeared and all(json.loads(e["details_json"])["baseline"] for e in appeared)

    world.update()
    data = fetch(cli, world)
    new_titles = {j["title"] for j in data["new_jobs"]}
    assert "Embedded Software Intern, Summer 2027" in new_titles
    assert "Cloud Engineering Intern - Summer 2027" in new_titles
    later = [e for e in events(home, "appeared") if not json.loads(e["details_json"])["baseline"]]
    assert len(later) == data["new_jobs_total"] == 3


def test_closures_changes_and_reappearance(cli, persona_home, world):
    home = persona_home("intl-sponsorship")
    fetch(cli, world)
    before = counts(home)["jobs"]
    world.update()
    data = fetch(cli, world)
    assert data["totals"]["closed"] == 2  # Greenhouse 1003 and the Workday row went inactive
    assert data["totals"]["changed"] == 1  # 1001 pay changed

    c = db(home)
    closed = c.execute("SELECT dedupe_key, live, closed_at FROM jobs WHERE live=0").fetchall()
    c.close()
    assert {r["dedupe_key"] for r in closed} == {
        "greenhouse:northwind:1003",
        "url:https://harborlight.wd5.myworkdayjobs.com/en-US/careers/job/Charlotte-NC/Technology-Intern_R-10023",
    }
    assert all(r["closed_at"] for r in closed)
    assert counts(home)["jobs"] == before + 3  # nothing deleted; three new
    changed = events(home, "changed")
    assert [json.loads(e["details_json"])["fields"] for e in changed] == [["pay"]]
    assert len(events(home, "disappeared")) == 2

    world.reset()  # back to the first snapshot: closed jobs reappear
    data = fetch(cli, world)
    assert data["totals"]["reopened"] == 2
    assert {e["dedupe_key"] for e in events(home, "reappeared")} == {
        "greenhouse:northwind:1003",
        "url:https://harborlight.wd5.myworkdayjobs.com/en-US/careers/job/Charlotte-NC/Technology-Intern_R-10023",
    }


def test_events_table_stays_append_only(cli, persona_home, world):
    home = persona_home("intl-sponsorship")
    fetch(cli, world)
    c = db(home)
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM events")
    c.close()


def test_signals(cli, persona_home, world):
    home = persona_home("intl-sponsorship")
    data = fetch(cli, world)
    assert data["new_signals"]["ai_instructions"] == 1
    assert data["new_signals"]["evergreen_language"] == 1
    assert data["new_signals"]["missing_from_ats"] == 1  # Simplify lists 1099; the board doesn't
    assert data["new_signals"]["old_posting"] >= 2

    c = db(home)
    sig = {
        (r["dedupe_key"], r["kind"]): r["evidence"]
        for r in c.execute(
            "SELECT j.dedupe_key, s.kind, s.evidence FROM signals s JOIN jobs j ON j.id=s.job_id"
        )
    }
    c.close()
    assert "large language model" in sig[("greenhouse:northwind:1002", "ai_instructions")]
    assert ("greenhouse:northwind:1004", "old_posting") not in sig  # undated: never "old"
    assert ("greenhouse:northwind:1099", "missing_from_ats") in sig

    world.update()
    data = fetch(cli, world)
    assert data["new_signals"].get("repost") == 1
    repost = next(j for j in data["new_jobs"] if "repost" in j["signals"])
    assert repost["title"] == "Future Opportunities - Robotics Software"


def test_ai_instructions_reach_the_user(cli, persona_home, world):
    persona_home("intl-sponsorship")
    code, env, _ = cli("fetch", "--fixtures", str(world.path))
    assert code == 0
    assert any("aimed at AI tools" in a for a in env["hints"]["user_actions"])


def test_same_title_same_run_is_not_a_repost(cli, persona_home, world):
    home = persona_home("senior-backend")
    fetch(cli, world)
    c = db(home)
    n = c.execute("SELECT count(*) FROM signals WHERE kind='repost'").fetchone()[0]
    c.close()
    assert n == 0


# --- failures and empty sources ---------------------------------------------------------------


def test_source_error_is_logged_and_other_sources_still_run(cli, persona_home, world):
    home = persona_home("senior-backend")
    (world.path / "api.lever.co/v0/postings/driftwood.json").unlink()
    data = fetch(cli, world)
    by_id = {s["source_id"]: s for s in data["sources"]}
    assert by_id["lever:driftwood"]["status"] == "error"
    assert "404" in by_id["lever:driftwood"]["error_detail"]
    assert by_id["greenhouse:tessellate"]["status"] == "ok"
    assert any("lever:driftwood failed" in w for w in data["warnings"])
    c = db(home)
    row = c.execute("SELECT errors FROM source_yields WHERE source_id='lever:driftwood'").fetchone()
    c.close()
    assert row["errors"] == 1


def test_all_sources_failing_is_a_network_exit(cli, persona_home, tmp_path):
    persona_home("senior-backend")
    empty = tmp_path / "empty"
    empty.mkdir()
    code, env, _ = cli("fetch", "--fixtures", str(empty))
    assert code == ExitCode.NETWORK
    assert env["error"]["code"] == "sources_failed"


def test_empty_source_holds_closures_then_warns(cli, persona_home, world):
    home = persona_home("senior-backend")
    fetch(cli, world)
    kw = world.path / "api.ashbyhq.com/posting-api/job-board/kitewire.json"
    kw.write_text('{"apiVersion": "1", "jobs": []}')
    for run in (1, 2):
        data = fetch(cli, world)
        y = next(s for s in data["sources"] if s["source_id"] == "ashby:kitewire")
        assert y["empty_runs"] == run and y["closures_held"] and y["closed"] == 0
    data = fetch(cli, world)
    y = next(s for s in data["sources"] if s["source_id"] == "ashby:kitewire")
    assert y["empty_runs"] == 3 and y["closed"] == 1
    assert any("returned no jobs 3 runs in a row" in w for w in data["warnings"])
    c = db(home)
    assert c.execute("SELECT live FROM jobs WHERE ats='ashby'").fetchone()[0] == 0
    c.close()


# --- CLI behaviour ----------------------------------------------------------------------------


def test_dry_run_writes_nothing(cli, persona_home, world, isolated_home):
    persona_home("intl-sponsorship")
    code, env, _ = cli("fetch", "--fixtures", str(world.path), "--dry-run")
    assert code == 0 and env["data"]["dry_run"] and env["data"]["totals"]["new"] > 0
    assert not (isolated_home / "jobhunt.db").exists()
    fetch(cli, world)
    before = counts(isolated_home)
    world.update()
    code, env, _ = cli("fetch", "--fixtures", str(world.path), "--dry-run")
    assert env["data"]["totals"]["new"] == 3
    assert counts(isolated_home) == before


def test_source_filter(cli, persona_home, world):
    persona_home("intl-sponsorship")
    data = fetch(cli, world, "--source", "ashby:quillstack")
    assert [s["source_id"] for s in data["sources"]] == ["ashby:quillstack"]
    code, _env, _ = cli("fetch", "--fixtures", str(world.path), "--source", "nope:x")
    assert code == ExitCode.VALIDATION


def test_fetch_requires_onboarding(cli, world):
    code, env, _ = cli("fetch", "--fixtures", str(world.path))
    assert code == ExitCode.CONFIG
    assert env["error"]["code"] == "onboarding_incomplete"


def test_human_output(capsys, persona_home, world):
    from jobhunt.cli.main import run

    persona_home("senior-backend")
    assert run(["fetch", "--fixtures", str(world.path), "--human"]) == 0
    out = capsys.readouterr().out
    assert "greenhouse:tessellate" in out and "(baseline)" in out


# --- jobhunt add ------------------------------------------------------------------------------


def test_add_supported_posting_and_readd_is_idempotent(cli, persona_home, world):
    home = persona_home("newgrad-analyst")
    url = "https://boards.greenhouse.io/ledgerline/jobs/2002?gh_src=linkedin"
    code, env, _ = cli("add", url, "--fixtures", str(world.path))
    assert code == 0, env
    d = env["data"]
    assert d["created"] and d["dedupe_key"] == "greenhouse:ledgerline:2002"
    assert d["company"] == "Ledgerline Financial" and d["posted_at"] == "2026-09-15T14:00:00Z"
    first = counts(home)
    code, env, _ = cli(
        "add",
        "https://job-boards.greenhouse.io/ledgerline/jobs/2002",
        "--fixtures",
        str(world.path),
    )
    assert code == 0 and not env["data"]["created"] and env["data"]["job_id"] == d["job_id"]
    assert counts(home)["jobs"] == first["jobs"]

    # The board fetch later finds the same job: merged, not duplicated.
    fetch(cli, world)
    c = db(home)
    n = c.execute("SELECT count(*) FROM jobs WHERE ats_job_id='2002'").fetchone()[0]
    c.close()
    assert n == 1


def test_add_unsupported_needs_company_and_title(cli, persona_home, world):
    persona_home("newgrad-analyst")
    url = "https://harborlight.wd5.myworkdayjobs.com/en-US/careers/job/Charlotte-NC/Risk-Analyst_R-1?utm_source=x"
    code, env, _ = cli("add", url, "--fixtures", str(world.path))
    assert code == ExitCode.USAGE and env["error"]["code"] == "missing_posting_info"
    code, env, _ = cli(
        "add",
        url,
        "--company",
        "Harborlight Bank",
        "--title",
        "Risk Analyst",
        "--fixtures",
        str(world.path),
    )
    assert code == 0
    d = env["data"]
    assert d["ats"] == "workday" and not d["supported"] and d["posted_at"] is None
    assert (
        d["dedupe_key"].startswith("url:https://harborlight.wd5.myworkdayjobs.com/")
        and "utm" not in d["dedupe_key"]
    )


def test_add_board_url_points_to_watchlist(cli, persona_home, world):
    persona_home("newgrad-analyst")
    code, env, _ = cli("add", "https://jobs.lever.co/bluefin", "--fixtures", str(world.path))
    assert code == ExitCode.USAGE and "watchlist" in env["hints"]["next_actions"][0]


def test_add_closed_posting_is_not_found(cli, persona_home, world):
    persona_home("newgrad-analyst")
    code, _env, _ = cli(
        "add", "https://boards.greenhouse.io/ledgerline/jobs/9999", "--fixtures", str(world.path)
    )
    assert code == ExitCode.NOT_FOUND


def test_added_jobs_are_repolled_and_closed(cli, persona_home, world):
    home = persona_home("newgrad-analyst")
    url = f"https://jobs.lever.co/bluefin/{BF_DS}"
    code, env, _ = cli("add", url, "--fixtures", str(world.path))
    assert code == 0 and env["data"]["locations"] == ["New York, NY", "Chicago, IL"]
    data = fetch(cli, world)
    manual = next(s for s in data["sources"] if s["source_id"] == "manual")
    assert manual["status"] == "ok" and manual["closed"] == 0
    (world.path / f"api.lever.co/v0/postings/bluefin/{BF_DS}.json").unlink()
    data = fetch(cli, world)
    manual = next(s for s in data["sources"] if s["source_id"] == "manual")
    assert manual["closed"] == 1
    c = db(home)
    row = c.execute("SELECT live FROM jobs WHERE ats_job_id=?", (BF_DS,)).fetchone()
    c.close()
    assert row["live"] == 0


def test_add_dry_run_writes_nothing(cli, persona_home, world, isolated_home):
    persona_home("newgrad-analyst")
    code, env, _ = cli(
        "add",
        "https://boards.greenhouse.io/ledgerline/jobs/2001",
        "--fixtures",
        str(world.path),
        "--dry-run",
    )
    assert code == 0 and env["data"]["created"] and env["data"]["dry_run"]
    assert not (isolated_home / "jobhunt.db").exists()


def test_add_when_ats_api_does_not_serve_the_board(cli, persona_home, world):
    """The job page exists but the ATS API doesn't know the board slug: ask for company and
    title instead of reporting a network failure, then key the job by URL."""
    persona_home("newgrad-analyst")
    url = f"https://jobs.ashbyhq.com/unknown-board/{BF_DS}"
    code, env, _ = cli("add", url, "--fixtures", str(world.path))
    assert code == ExitCode.USAGE and "public API" in env["error"]["message"]
    code, env, _ = cli(
        "add",
        url,
        "--company",
        "Unknown Board Co",
        "--title",
        "Analyst",
        "--fixtures",
        str(world.path),
    )
    assert code == 0
    assert env["data"]["dedupe_key"] == f"url:https://jobs.ashbyhq.com/unknown-board/{BF_DS}"
