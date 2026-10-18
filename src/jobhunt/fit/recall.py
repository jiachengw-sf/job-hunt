"""Stage 1: recall. Search the de-identified resume against postings, locally.

BM25 always runs. When the optional ``[recall]`` extra (sentence-transformers) is installed and
``fit.use_embeddings`` is ``auto``, a small sentence-embedding model ranks too, and the two
rankings are fused with reciprocal rank fusion. The result is *relevance*, never "fit".
"""

from __future__ import annotations

import array
import hashlib
import importlib.util
import logging
import math
import re
import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from jobhunt.fit.jobview import JobView
from jobhunt.models import Preferences
from jobhunt.models.config import FitConfig

log = logging.getLogger("jobhunt.recall")

RRF_K = 60
_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#]*(?:\.[a-z0-9]+)?")
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "our",
        "that",
        "the",
        "their",
        "this",
        "to",
        "we",
        "will",
        "with",
        "you",
        "your",
        "they",
        "them",
        "who",
        "what",
        "when",
        "where",
        "how",
        "all",
        "any",
        "can",
        "may",
        "must",
        "should",
        "would",
        "able",
        "work",
        "working",
        "team",
        "role",
        "job",
        "position",
        "company",
        "experience",
        "years",
        "year",
        "including",
        "include",
        "etc",
        "per",
        "new",
        "using",
        "use",
        "used",
        "about",
        "across",
        "also",
        "more",
        "most",
        "other",
        "such",
        "within",
        "strong",
        "ability",
        "skills",
    ]
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS and len(t) > 1]


class BM25:
    def __init__(self, docs: Sequence[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.tf = [Counter(d) for d in docs]
        self.len = [len(d) for d in docs]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 0.0
        df: Counter[str] = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        q = Counter(query)
        out = []
        for tf, dl in zip(self.tf, self.len, strict=True):
            s = 0.0
            norm = self.k1 * (1 - self.b + self.b * dl / self.avg) if self.avg else self.k1
            for term, qn in q.items():
                f = tf.get(term)
                if f:
                    s += self.idf[term] * f * (self.k1 + 1) / (f + norm) * min(qn, 3)
            out.append(s)
        return out


def job_document(job: JobView) -> str:
    """Title weighted three times; the description is capped so long postings don't dominate."""
    return "\n".join([job.title] * 3 + job.categories + [job.description[:4000]])


def query_text(resume: str, prefs: Preferences) -> str:
    extra = []
    if prefs.roles.families:
        extra += list(prefs.roles.families.value) * 2
    if prefs.roles.include_keywords:
        extra += list(prefs.roles.include_keywords.value)
    return "\n".join([resume, *extra])


def rrf(rankings: list[list[int]], k: int = RRF_K) -> dict[int, float]:
    out: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            out[item] = out.get(item, 0.0) + 1.0 / (k + rank)
    return out


# --- optional embeddings --------------------------------------------------------------------


def embeddings_available() -> bool:
    return importlib.util.find_spec("sentence_transformers") is not None


class Embedder:
    """Wraps sentence-transformers; vectors are cached per job in the store."""

    def __init__(self, model: str) -> None:
        from sentence_transformers import SentenceTransformer  # optional extra

        self.name = model
        self.model = SentenceTransformer(model)

    def encode(self, texts: list[str]) -> list[list[float]]:
        vecs: Any = self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [list(map(float, v)) for v in vecs]


def _pack(v: list[float]) -> bytes:
    return array.array("f", v).tobytes()


def _unpack(b: bytes) -> list[float]:
    a = array.array("f")
    a.frombytes(b)
    return list(a)


def _cached_vectors(
    conn: sqlite3.Connection, embedder: Embedder, jobs: list[JobView]
) -> dict[int, list[float]]:
    want = {j.id: job_document(j)[:2000] for j in jobs}
    hashes = {jid: hashlib.sha256(t.encode()).hexdigest()[:24] for jid, t in want.items()}
    have: dict[int, list[float]] = {}
    for r in conn.execute(
        "SELECT job_id, text_hash, vector FROM embeddings WHERE model=?", (embedder.name,)
    ):
        if hashes.get(r["job_id"]) == r["text_hash"]:
            have[r["job_id"]] = _unpack(r["vector"])
    missing = [jid for jid in want if jid not in have]
    for i in range(0, len(missing), 64):
        batch = missing[i : i + 64]
        for jid, vec in zip(batch, embedder.encode([want[j] for j in batch]), strict=True):
            have[jid] = vec
            conn.execute(
                "INSERT OR REPLACE INTO embeddings (job_id, model, text_hash, vector) VALUES "
                "(?,?,?,?)",
                (jid, embedder.name, hashes[jid], _pack(vec)),
            )
    return have


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


# --- recall ---------------------------------------------------------------------------------


@dataclass
class Recalled:
    rank: int
    score: float  # fused relevance (higher is more relevant); never shown as fit
    recalled: bool  # within top K, or kept because the company is on the watchlist


@dataclass
class RecallResult:
    mode: str  # "bm25" | "bm25+embeddings"
    by_job: dict[int, Recalled]
    note: str | None = None


def recall(
    conn: sqlite3.Connection,
    jobs: list[JobView],
    resume: str,
    prefs: Preferences,
    cfg: FitConfig,
    *,
    watch_companies: set[str],
    embedder: Embedder | None = None,
) -> RecallResult:
    if not jobs:
        return RecallResult("bm25", {})
    query = query_text(resume, prefs)
    bm = BM25([tokenize(job_document(j)) for j in jobs]).scores(tokenize(query))
    order_bm = [jobs[i].id for i in sorted(range(len(jobs)), key=lambda i: (-bm[i], jobs[i].id))]
    rankings = [order_bm]
    mode, note = "bm25", None
    if embedder is not None:
        try:
            qv = embedder.encode([query[:2000]])[0]
            vecs = _cached_vectors(conn, embedder, jobs)
            sims = {jid: _dot(qv, v) for jid, v in vecs.items()}
            rankings.append(sorted(sims, key=lambda jid: (-sims[jid], jid)))
            mode = "bm25+embeddings"
        except Exception as e:  # a broken model install must not stop the run
            log.warning("embedding recall failed, using BM25 only: %s", e)
            note = f"embedding recall failed ({type(e).__name__}); BM25 only"
    fused = rrf(rankings)
    ordered = sorted(fused, key=lambda jid: (-fused[jid], jid))
    watch = {c.lower() for c in watch_companies}
    company = {j.id: j.company.lower() for j in jobs}
    out = {}
    for rank, jid in enumerate(ordered, start=1):
        keep = rank <= cfg.recall_top_k or company[jid] in watch
        out[jid] = Recalled(rank=rank, score=round(fused[jid], 6), recalled=keep)
    return RecallResult(mode, out, note)


def make_embedder(cfg: FitConfig) -> tuple[Embedder | None, str | None]:
    """The embedder to use, or None with a reason."""
    if cfg.use_embeddings == "off":
        return None, "embeddings off in config"
    if not embeddings_available():
        return None, "install the [recall] extra for embedding recall; using BM25 only"
    try:
        return Embedder(cfg.embedding_model), None
    except Exception as e:
        return None, f"could not load {cfg.embedding_model} ({type(e).__name__}); using BM25 only"
