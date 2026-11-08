"""The ranking: tier, then Nice preferences met, then date; each job with its reason."""

from __future__ import annotations

import sqlite3

import pytest

from jobhunt.cli.fit import _render_jobs
from jobhunt.config import load_config
from jobhunt.envelope import Envelope
from jobhunt.errors import ExitCode
from jobhunt.fit.rank import (
    TIER_LABEL,
    TIERS,
    RankInput,
    Soft,
    date_key,
    rank,
    role_match,
    soft_matches,
    tier,
    title_related,
)
from jobhunt.fit.recall import recall
from jobhunt.fit.scoring import preference
from jobhunt.paths import Paths
from tests.conftest import REPO
from tests.fit_helpers import TODAY, job, persona_ctx

CFG = persona_ctx("first-year-intern").cfg
PERSONAS = ["first-year-intern", "newgrad-analyst", "intl-sponsorship", "senior-backend"]


# --- role match ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("persona", "title", "want"),
    [
        ("first-year-intern", "Software Engineer, Intern", "software engineering"),
        ("first-year-intern", "SWE Intern", "software engineering"),
        ("first-year-intern", "Backend Engineering Intern", "software engineering"),
        ("first-year-intern", "Data Engineering Intern", "data engineering"),
        ("first-year-intern", "Account Executive", None),
        ("first-year-intern", "Mechanical Engineering Intern", None),
        ("first-year-intern", "Data Analyst", None),  # "data" alone isn't data engineering
        ("intl-sponsorship", "ML Engineer Intern", "machine learning engineering"),
        ("newgrad-analyst", "Data Analyst, New Grad", "data analytics"),
        ("newgrad-analyst", "Business Intelligence Analyst I", "business intelligence"),
        ("newgrad-analyst", "Software Engineer, New Grad", None),
        ("senior-backend", "Staff Software Engineer, Infrastructure", "platform engineering"),
        ("senior-backend", "Senior Software Engineer, Distributed Systems", "backend engineering"),
    ],
)
def test_role_match(persona, title, want):
    assert role_match(title, persona_ctx(persona).prefs) == want


# --- tiers -----------------------------------------------------------------------------------


def test_tiers():
    t = CFG.tiers
    assert tier(t.strong, "swe", True, CFG) == ("strong", None)
    assert tier(1.0, "swe", False, CFG) == ("possible", "title only")  # never strong untexted
    assert tier(t.strong - 0.01, "swe", True, CFG)[0] == "possible"
    assert tier(t.possible, None, True, CFG)[0] == "possible"  # related wording, no role match
    assert tier(t.possible - 0.01, None, True, CFG)[0] == "weak"
    assert tier(1.0, None, True, CFG)[0] == "possible"  # strong needs a role match
    # boilerplate text can't lift a title that shares no word with your search
    assert tier(1.0, None, True, CFG, related=False)[0] == "weak"
    assert title_related("Account Executive", frozenset({"python", "intern"})) is False
    assert title_related("Software Intern", frozenset({"python", "intern"})) is True


# --- soft preferences ------------------------------------------------------------------------


def test_soft_matches_count_each_nice_preference_once():
    prefs = persona_ctx("first-year-intern").prefs
    pay = {"min": 30, "max": 40, "interval": "per-hour-wage"}
    j = job("Software Engineering Intern, Summer 2027", work_mode="hybrid", pay=pay)
    p = preference(j, prefs, CFG)
    s = soft_matches(p.components, p.labels, referral=False, watchlist=True)
    assert s.met == ["Hybrid ✓", "Chicago ✓", "Pay ✓", "Summer 2027 ✓", '"intern" ✓', "watchlist ✓"]
    assert s.count == 6


def test_remote_counts_once_and_a_nice_exclude_hit_counts_against():
    prefs = persona_ctx("intl-sponsorship").prefs  # nice exclude: senior, staff, manager
    j = job("Senior Software Engineer Intern", locations=["Remote - US"], work_mode="remote")
    p = preference(j, prefs, CFG)
    s = soft_matches(p.components, p.labels, referral=True, watchlist=False)
    assert '"senior" ✗' in s.missed
    assert s.count == len(s.met) - 1
    prefs = persona_ctx("first-year-intern").prefs  # remote allowed, Chicago place
    p = preference(job(locations=["Remote - US"], work_mode="remote"), prefs, CFG)
    s = soft_matches(p.components, p.labels, referral=False, watchlist=False)
    assert s.met.count("Remote ✓") == 1  # work mode and location both met by remote


# --- date ------------------------------------------------------------------------------------


def dated(posted=None, deadline=None, title="Software Engineering Intern", desc=""):
    j = job(title, desc)
    j.posted_at, j.deadline = posted, deadline
    return j


def test_date_buckets():
    assert date_key(dated("2026-10-16T00:00:00Z"), TODAY, CFG) == (1, 2, "posted 2 days ago")
    assert date_key(dated(None), TODAY, CFG)[::2] == (2, "date unknown")
    evergreen = dated("2025-09-01T00:00:00Z", desc="We hire interns throughout the year.")
    assert date_key(evergreen, TODAY, CFG)[::2] == (2, "always open")  # old date, not "stale"
    assert date_key(dated("2026-08-01T00:00:00Z"), TODAY, CFG)[0] == 3  # known and stale
    due = dated("2026-09-01T00:00:00Z", deadline="2026-10-28T00:00:00Z")
    assert date_key(due, TODAY, CFG) == (0, 10, "apply by Oct 28")
    full_time = dated("2026-10-16T00:00:00Z", deadline="2026-10-28", title="Data Analyst")
    assert date_key(full_time, TODAY, CFG)[0] == 1  # deadlines lead only for internships
    passed = dated("2026-10-16T00:00:00Z", deadline="2026-10-01T00:00:00Z")
    assert date_key(passed, TODAY, CFG)[0] == 3


def test_unknown_dates_sort_between_recent_and_stale():
    recent = dated("2026-10-10T00:00:00Z")
    unknown = dated(None)
    stale = dated("2026-06-01T00:00:00Z")
    keys = [date_key(j, TODAY, CFG)[:2] for j in (stale, unknown, recent)]
    assert sorted(keys) == [keys[2], keys[1], keys[0]]


# --- all together ----------------------------------------------------------------------------


def item(jid, rel=0.9, role="software engineering", text=True, soft=0, posted=None, **kw):
    j = job(jid=jid)
    j.posted_at = posted
    return RankInput(j, rel, role, text, Soft(soft), **kw)


def order(items):
    r = rank(items, TODAY, CFG)
    return [jid for jid in sorted(r, key=lambda j: r[j].rank)], r


def test_keys_apply_in_order():
    ids, _ = order([
        item(1, rel=0.05, role=None, soft=9, posted="2026-10-18T00:00:00Z"),  # weak
        item(2, soft=1, posted="2026-10-18T00:00:00Z"),  # strong, 1 soft
        item(3, soft=2, posted="2026-05-01T00:00:00Z"),  # strong, 2 soft, stale
        item(4, soft=2, posted="2026-10-17T00:00:00Z"),  # strong, 2 soft, recent
        item(5, text=False, soft=5),  # title only: possible
    ])  # fmt: skip
    assert ids == [4, 3, 2, 5, 1]


def test_demotions_and_the_skip_filter():
    ids, r = order([
        item(1, band="not_a_fit"),
        item(2, degree_above="a PhD"),
        item(3, form_filter="deprioritize"),
        item(4, form_filter="skip", soft=9),
        item(5),
    ])  # fmt: skip
    assert ids[0] == 5 and ids[-1] == 4
    assert r[1].tier == "weak" and r[2].tier == "possible" and r[3].tier == "possible"
    assert "lists a PhD" in r[2].reason and "your form filter skips it" in r[4].reason


def test_reason_reads_like_the_keys():
    prefs = persona_ctx("senior-backend").prefs
    j = job("Senior Backend Engineer", "Go and Postgres on our payments platform. " * 3,
            locations=["Remote - US"], work_mode="remote",
            pay={"min": 200000, "max": 240000, "interval": "per-year-salary"})  # fmt: skip
    j.posted_at = "2026-10-16T00:00:00Z"
    p = preference(j, prefs, CFG)
    s = soft_matches(p.components, p.labels, referral=False, watchlist=False)
    r = rank([RankInput(j, 0.9, role_match(j.title, prefs), True, s)], TODAY, CFG)[j.id]
    assert r.reason == 'Strong match · Remote ✓ · Pay ✓ · "backend" ✓ · posted 2 days ago'


def test_the_stripe_case_a_watchlist_internship_beats_a_fresh_sales_job():
    """Both at a watchlist company; the internship is an evergreen post, the sales job new."""
    ctx = persona_ctx("first-year-intern")
    ae = job("Account Executive", "Own the full sales cycle. " * 20, jid=1, company="Quill")
    ae.posted_at = "2026-10-17T00:00:00Z"
    intern = job("Software Engineer, Intern", "Interns write Python with a mentor. " * 20, jid=2,
                 company="Quill", employment_type="Intern")  # fmt: skip
    intern.posted_at = "2025-09-01T00:00:00Z"
    fillers = [job(f"Software Engineer Intern {i}", jid=10 + i) for i in range(80)]
    jobs = [ae, intern, *fillers]
    cfg = ctx.cfg.model_copy(update={"recall_top_k": 10})
    rec = recall(sqlite3.connect(":memory:"), jobs, "python mentor software engineer intern",
                 ctx.prefs, cfg, watch_companies={"Quill"})  # fmt: skip
    assert rec.relevance[2] > 0  # past top K, still scored
    items = []
    for j in (ae, intern):
        p = preference(j, ctx.prefs, cfg)
        s = soft_matches(p.components, p.labels, referral=False, watchlist=True)
        rel = rec.relevance[j.id]
        items.append(RankInput(j, rel, role_match(j.title, ctx.prefs), True, s))
    r = rank(items, TODAY, cfg)
    assert r[2].rank < r[1].rank and r[1].tier == "weak"


def test_title_only_rows_are_never_strong_for_any_persona():
    for name in PERSONAS:
        ctx = persona_ctx(name)
        fam = ctx.prefs.roles.families.value[0]
        assert tier(1.0, fam, False, ctx.cfg)[0] == "possible", name


def test_old_priority_settings_still_load(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "fit:\n  priority_threshold: 0.4\n  priority: {band: 0.5}\n"
        "  freshness_half_life_days: 3\nnotifications:\n  alert_priority: 0.9\n",
        encoding="utf-8",
    )
    cfg = load_config(Paths(tmp_path))
    assert cfg.fit.ready_tier == "strong" and cfg.fit.tiers.strong == 0.5


# --- where the ranking shows -----------------------------------------------------------------

BASE = str(REPO / "tests" / "fixtures" / "sources" / "base")


@pytest.mark.parametrize("persona", PERSONAS)
def test_jobs_list_in_rank_order_with_reasons(cli, persona_home, persona):
    persona_home(persona)
    code, env, _ = cli("run", "--fixtures", BASE, "--no-sync", "--no-inspect")
    assert code == ExitCode.OK, env
    m = env["data"]["match"]
    assert sum(m["tiers"].values()) == m["gates"]["passed"] + m["gates"]["unclear"]
    assert [j["rank"] for j in m["top"]] == sorted(j["rank"] for j in m["top"])
    _, env, _ = cli("jobs", "--limit", "200")
    rows = env["data"]["jobs"]
    assert rows and [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
    tiers = [TIERS.index(r["tier"]) for r in rows]
    skipped = [r for r in rows if r["form_filter"] == "skip"]
    assert tiers[: len(rows) - len(skipped)] == sorted(tiers[: len(rows) - len(skipped)])
    for r in rows:
        assert r["reason"].startswith(TIER_LABEL[r["tier"]])
    _, env, _ = cli("show", str(rows[0]["id"]))
    assert env["data"]["fit"]["rank"] == 1 and env["data"]["fit"]["rank_reason"]


def test_hide_weak_leaves_weak_matches_out_of_jobs(cli, persona_home):
    home = persona_home("first-year-intern")
    cli("run", "--fixtures", BASE, "--no-sync", "--no-inspect")
    _, env, _ = cli("jobs", "--limit", "500")
    assert any(r["tier"] == "weak" for r in env["data"]["jobs"])
    (home / "config.yaml").write_text("fit:\n  hide_weak: true\n", encoding="utf-8")
    _, env, _ = cli("jobs", "--limit", "500")
    assert env["data"]["jobs"] and all(r["tier"] != "weak" for r in env["data"]["jobs"])


def test_jobs_human_shows_the_reason():
    env = Envelope(ok=True, data={"jobs": [
        {"id": 7, "company": "Acme", "title": "SWE Intern", "rank": 1,
         "reason": "Strong match · Remote ✓ · posted 2 days ago", "gate_reasons": []},
        {"id": 8, "company": "Beta", "title": "Sales", "rank": None, "reason": None,
         "gate_reasons": ["country: located in DE"]},
    ]})  # fmt: skip
    text = _render_jobs(env)
    assert (
        "   1. #7     Acme: SWE Intern\n       Strong match · Remote ✓ · posted 2 days ago" in text
    )
    assert "gated out: country: located in DE" in text


def test_doctor_names_settings_ranking_ignores(tmp_path):
    from jobhunt.doctor import check_config

    (tmp_path / "config.yaml").write_text(
        "fit:\n  priority_threshold: 0.4\nnotifications:\n  alert_priority: 0.9\n", "utf-8"
    )
    c = check_config(Paths(tmp_path))
    assert c.status == "warn" and "fit.priority_threshold" in c.summary
    assert "notifications.alert_priority" in c.summary
