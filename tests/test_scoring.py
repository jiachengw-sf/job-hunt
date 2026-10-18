from __future__ import annotations

import sqlite3

from jobhunt.fit.recall import BM25, recall, rrf, tokenize
from jobhunt.fit.requirements import Requirement
from jobhunt.fit.scoring import preference, priority, qualify
from tests.fit_helpers import TODAY, job, persona_ctx


def req(importance="must", status=None, tier=None, seq=0):
    return Requirement(seq=seq, text=f"r{seq}", importance=importance, type="skill", section="",
                       status=status, tier=tier)  # fmt: skip


CFG = persona_ctx("first-year-intern").cfg


def q(reqs, need=None, have=0.0):
    return qualify(reqs, [], CFG, required_years=need, your_years=have)


def test_all_musts_stated_is_strong_and_final():
    r = q([req(status="met", tier="stated", seq=i) for i in range(3)])
    assert r.band == "strong" and not r.provisional


def test_unmet_must_caps_at_stretch():
    reqs = [req(status="met", tier="stated", seq=i) for i in range(5)] + [
        req(status="unmet", tier="none", seq=9)
    ]
    r = q(reqs)
    assert r.band == "stretch"
    assert any("must-have is unmet" in x for x in r.rules)


def test_inferred_must_never_makes_strong():
    reqs = [req(status="met", tier="stated", seq=0), req(status="met", tier="inferred", seq=1)]
    r = q(reqs)
    assert r.band != "strong"


def test_unassessed_is_provisional_and_not_strong():
    r = q([req(status="met", tier="stated", seq=0), req(seq=1)])
    assert r.provisional and r.band != "strong"


def test_level_mismatch_caps_at_stretch():
    r = q([req(status="met", tier="stated")], need=8, have=1)
    assert r.band == "stretch" and any("level mismatch" in x for x in r.rules)


def test_no_requirements_means_no_band():
    r = q([])
    assert r.band is None and r.provisional


def test_preference_pay_and_missing_pay_flag():
    prefs = persona_ctx("newgrad-analyst").prefs
    pay = {"min": 78000, "max": 85000, "interval": "per-year-salary"}
    good = preference(
        job("Data Analyst", pay=pay, locations=["Boston, MA"], work_mode="hybrid"), prefs, CFG
    )
    none = preference(job("Data Analyst", locations=["Boston, MA"], work_mode="hybrid"), prefs, CFG)
    assert good.components["pay"] == 1.0 and good.components["location"] == 1.0
    assert none.components["pay"] is None and "pay not listed" in none.flags


def test_priority_prefers_better_band_and_fresher():
    j = job()
    hi, _ = priority(j, "strong", 0.8, CFG, TODAY, referral=False, watchlist=False)
    lo, _ = priority(j, "stretch", 0.8, CFG, TODAY, referral=False, watchlist=False)
    assert hi.score > lo.score
    old = job()
    old.posted_at = "2026-08-01T00:00:00Z"
    stale, _ = priority(old, "strong", 0.8, CFG, TODAY, referral=False, watchlist=False)
    assert stale.score < hi.score
    ref, _ = priority(j, "strong", 0.8, CFG, TODAY, referral=True, watchlist=False)
    assert ref.score > hi.score


def test_bm25_and_rrf():
    docs = [
        tokenize("python flask web intern"),
        tokenize("accounting payroll clerk"),
        tokenize("python data"),
    ]
    s = BM25(docs).scores(tokenize("python flask"))
    assert s[0] > s[2] > s[1]
    fused = rrf([[1, 2, 3], [3, 1, 2]])
    assert max(fused, key=fused.get) == 1


def test_recall_top_k_and_watchlist_kept():
    ctx = persona_ctx("first-year-intern")
    cfg = ctx.cfg.model_copy(update={"recall_top_k": 1})
    jobs = [
        job("Python Flask Intern", "Python Flask SQLite pytest", jid=1),
        job("Payroll Clerk", "accounting payroll", jid=2, company="Watched Co"),
        job("Barista", "coffee", jid=3),
    ]
    conn = sqlite3.connect(":memory:")
    res = recall(
        conn, jobs, "Python Flask SQLite pytest", ctx.prefs, cfg, watch_companies={"Watched Co"}
    )
    assert res.by_job[1].rank == 1 and res.by_job[1].recalled
    assert res.by_job[2].recalled  # watchlist company kept past the cutoff
    assert not res.by_job[3].recalled
    assert res.mode == "bm25"


class FakeEmbedder:
    """Deterministic stand-in for sentence-transformers: bag-of-keywords vectors."""

    name = "fake-model"
    KEYS = ["python", "flask", "payroll", "coffee"]

    def __init__(self):
        self.calls = 0

    def encode(self, texts):
        self.calls += len(texts)
        out = []
        for t in texts:
            v = [float(t.lower().count(k)) for k in self.KEYS]
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out


def test_embedding_recall_fuses_and_caches():
    from jobhunt.store.db import migrate

    ctx = persona_ctx("first-year-intern")
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    jobs = [
        job("Python Flask Intern", "python flask", jid=1),
        job("Payroll Clerk", "payroll", jid=2),
    ]
    for j in jobs:
        conn.execute(
            "INSERT INTO jobs (id, dedupe_key, company, title, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            (j.id, f"k{j.id}", j.company, j.title, "t", "t"),
        )
    emb = FakeEmbedder()
    res = recall(
        conn, jobs, "python flask", ctx.prefs, ctx.cfg, watch_companies=set(), embedder=emb
    )
    assert res.mode == "bm25+embeddings" and res.by_job[1].rank == 1
    first = emb.calls
    recall(conn, jobs, "python flask", ctx.prefs, ctx.cfg, watch_companies=set(), embedder=emb)
    assert emb.calls == first + 1  # only the query is re-encoded; job vectors come from the cache
    assert conn.execute("SELECT count(*) FROM embeddings").fetchone()[0] == 2


def test_broken_embedder_falls_back_to_bm25():
    class Broken:
        name = "broken"

        def encode(self, texts):
            raise RuntimeError("no model")

    ctx = persona_ctx("first-year-intern")
    res = recall(sqlite3.connect(":memory:"), [job(jid=1)], "python", ctx.prefs, ctx.cfg,
                 watch_companies=set(), embedder=Broken())  # fmt: skip
    assert res.mode == "bm25" and "BM25 only" in (res.note or "")
