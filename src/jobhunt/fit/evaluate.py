"""`jobhunt eval-fit`: measure each fit stage against labeled persona-posting pairs.

Reports per persona and macro-averaged:
  gates         accuracy, wrongly gated (the costly error), missed (kept but should be out)
  recall        nDCG@10 of the relevance ranking
  final         nDCG@10 of the priority ranking
  band          exact and within-one agreement with labeled bands
  requirements  extracted / assessed by code / accuracy where assessed
"""

from __future__ import annotations

import json
import math
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from jobhunt.config import USER_FILES, load_config, validate
from jobhunt.errors import NotFound
from jobhunt.fit.engine import Inputs, run_match
from jobhunt.fit.scoring import BANDS
from jobhunt.models import Answers, Config, Preferences, Profile, Watchlist
from jobhunt.paths import Paths
from jobhunt.store.db import migrate


def find_eval_set() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "evals" / "fit"
        if (cand / "manifest.yaml").is_file():
            return cand
    return None


def ndcg_at(ranked_grades: list[int], all_grades: list[int], k: int = 10) -> float | None:
    def dcg(gs: list[int]) -> float:
        return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(gs[:k]))

    ideal = dcg(sorted(all_grades, reverse=True))
    return round(dcg(ranked_grades) / ideal, 3) if ideal > 0 else None


def _load_persona(d: Path) -> tuple[Profile, Preferences, Answers]:
    out = []
    for kind in ("profile", "preferences", "answers"):
        path = d / f"{kind}.yaml"
        out.append(
            validate(USER_FILES[kind], yaml.safe_load(path.read_text(encoding="utf-8")), path)
        )
    return out[0], out[1], out[2]  # type: ignore[return-value]


def _insert_postings(
    conn: sqlite3.Connection, postings: list[dict[str, Any]], today: date
) -> dict[str, int]:
    ids = {}
    for p in postings:
        posted = (today - timedelta(days=p.get("posted_days_ago", 0))).isoformat() + "T00:00:00Z"
        cur = conn.execute(
            "INSERT INTO jobs (dedupe_key, ats, company, title, locations_json, work_mode, "
            "employment_type, "
            "description, pay_json, posted_at, first_seen, last_seen, live) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (
                f"eval:{p['id']}",
                "eval",
                p["company"],
                p["title"],
                json.dumps(p.get("locations", [])),
                p.get("work_mode"),
                p.get("employment_type"),
                p.get("description", ""),
                json.dumps(p["pay"]) if p.get("pay") else None,
                posted,
                posted,
                posted,
            ),
        )
        ids[p["id"]] = int(cur.lastrowid or 0)
    return ids


def _match_req(key: str, rows: list[sqlite3.Row]) -> sqlite3.Row | None:
    k = key.lower().strip()
    for r in rows:
        t = r["text"].lower().strip()
        if t.startswith(k) or t == k:
            return r
    for r in rows:
        if k in r["text"].lower():
            return r
    return None


def _persona_metrics(
    conn: sqlite3.Connection, ids: dict[str, int], labels: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    fits = {r["job_id"]: r for r in conn.execute("SELECT * FROM fits")}
    grade = {
        pid: (0 if lab.get("gated") else int(lab.get("grade", 0))) for pid, lab in labels.items()
    }
    # gates
    correct = wrongly_gated = missed_unclear = missed_passed = 0
    wrong_list, missed_list = [], []
    for pid, lab in labels.items():
        f = fits.get(ids[pid])
        pred = f is not None and f["gate_status"] == "failed"
        truth = bool(lab.get("gated"))
        if pred == truth:
            correct += 1
        elif pred and not truth:
            wrongly_gated += 1
            wrong_list.append(pid)
        else:
            missed_list.append(pid)
            if f is not None and f["gate_status"] == "unclear":
                missed_unclear += 1
            else:
                missed_passed += 1
    # rankings
    by_rel = sorted(labels, key=lambda pid: (-(fits[ids[pid]]["relevance"] or -1), pid))
    by_pri = sorted(labels, key=lambda pid: (-(fits[ids[pid]]["priority"] or 0), pid))
    all_g = list(grade.values())
    # bands
    exact = within = compared = null_band = 0
    for pid, lab in labels.items():
        if lab.get("gated") or "band" not in lab:
            continue
        f = fits[ids[pid]]
        if f["gate_status"] == "failed":
            continue
        if f["band"] is None:
            null_band += 1
            continue
        compared += 1
        d = abs(BANDS.index(f["band"]) - BANDS.index(lab["band"]))
        exact += d == 0
        within += d <= 1
    # requirements
    labeled = extracted = assessed = req_correct = 0
    for pid, lab in labels.items():
        rows = conn.execute(
            "SELECT text, status FROM requirements WHERE job_id=?", (ids[pid],)
        ).fetchall()
        for key, status in (lab.get("req") or {}).items():
            labeled += 1
            r = _match_req(key, rows)
            if r is None:
                continue
            extracted += 1
            if r["status"] is None:
                continue
            assessed += 1
            req_correct += r["status"] == status
    n = len(labels)
    return {
        "pairs": n,
        "gates": {
            "accuracy": round(correct / n, 3),
            "wrongly_gated": wrongly_gated,
            "wrongly_gated_ids": wrong_list,
            "missed_unclear": missed_unclear,
            "missed_passed": missed_passed,
            "missed_ids": missed_list,
        },
        "recall_ndcg10": ndcg_at([grade[p] for p in by_rel], all_g),
        "final_ndcg10": ndcg_at([grade[p] for p in by_pri], all_g),
        "band": {
            "compared": compared,
            "exact": round(exact / compared, 3) if compared else None,
            "within_one": round(within / compared, 3) if compared else None,
            "no_band": null_band,
        },
        "requirements": {
            "labeled": labeled,
            "extracted": round(extracted / labeled, 3) if labeled else None,
            "assessed_by_code": round(assessed / labeled, 3) if labeled else None,
            "accuracy_where_assessed": round(req_correct / assessed, 3) if assessed else None,
        },
    }


def _mean(vals: list[float | None]) -> float | None:
    xs = [v for v in vals if v is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def evaluate_set(set_dir: Path, cfg: Config | None = None) -> dict[str, Any]:
    manifest = yaml.safe_load((set_dir / "manifest.yaml").read_text(encoding="utf-8"))
    today = date.fromisoformat(str(manifest["today"]))
    now = f"{today.isoformat()}T12:00:00Z"
    postings = yaml.safe_load((set_dir / manifest["postings"]).read_text(encoding="utf-8"))
    labels = yaml.safe_load((set_dir / manifest["labels"]).read_text(encoding="utf-8"))
    personas_dir = (set_dir / manifest["personas_dir"]).resolve()
    if cfg is None:
        with tempfile.TemporaryDirectory() as td:
            cfg = load_config(Paths(Path(td)))  # package defaults only
    cfg = cfg.model_copy(deep=True)
    cfg.fit.use_embeddings = "off"  # deterministic, no model download
    per: dict[str, Any] = {}
    for persona, plabels in labels.items():
        profile, prefs, answers = _load_persona(personas_dir / persona)
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        migrate(conn)
        ids = _insert_postings(conn, postings, today)
        missing = sorted(set(ids) - set(plabels))
        inp = Inputs(profile, prefs, answers, Watchlist(), cfg, today, now)
        with tempfile.TemporaryDirectory() as td:
            run_match(
                conn,
                inp,
                tasks_dir=Path(td),
                results_dir=Path(td),
                embedder=None,
                embed_note=None,
                dry_run=True,
            )
        per[persona] = _persona_metrics(conn, ids, plabels)
        if missing:
            per[persona]["unlabeled"] = missing
        conn.close()
    macro = {
        "gates_accuracy": _mean([p["gates"]["accuracy"] for p in per.values()]),
        "wrongly_gated": sum(p["gates"]["wrongly_gated"] for p in per.values()),
        "recall_ndcg10": _mean([p["recall_ndcg10"] for p in per.values()]),
        "final_ndcg10": _mean([p["final_ndcg10"] for p in per.values()]),
        "band_exact": _mean([p["band"]["exact"] for p in per.values()]),
        "band_within_one": _mean([p["band"]["within_one"] for p in per.values()]),
        "requirement_accuracy": _mean(
            [p["requirements"]["accuracy_where_assessed"] for p in per.values()]
        ),
        "requirements_assessed_by_code": _mean(
            [p["requirements"]["assessed_by_code"] for p in per.values()]
        ),
    }
    return {
        "set": str(set_dir),
        "postings": len(postings),
        "annotators": manifest.get("annotators"),
        "caveat": "Code-only stages: judgment tasks are not run, so bands stay provisional."
        + (
            " Single-annotator labels: a regression check, not evidence of accuracy."
            if manifest.get("annotators", 1) < 2
            else ""
        ),
        "macro": macro,
        "personas": per,
    }


def evaluate_personal(paths: Paths, cfg: Config) -> dict[str, Any]:
    """Your own labels: ~/.jobhunt/labels.yaml mapping job id -> grade (0, 1 or 2)."""
    path = paths.home / "labels.yaml"
    if not path.exists():
        raise NotFound(
            f"{path} does not exist.",
            next_actions=[
                f"Create {path} with lines like `123: 2` (job id: grade 0/1/2), then re-run."
            ],
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    labels = {int(k): int(v) for k, v in raw.items()}
    from jobhunt.store import open_store

    with open_store(paths.db, dry_run=True) as conn:
        rows = {r["job_id"]: r for r in conn.execute("SELECT job_id, priority, band FROM fits")}
    known = [j for j in labels if j in rows]
    by_pri = sorted(known, key=lambda j: (-(rows[j]["priority"] or 0), j))
    by_band: dict[str, list[int]] = {}
    for j in known:
        by_band.setdefault(rows[j]["band"] or "none", []).append(labels[j])
    return {
        "labeled": len(labels),
        "found": len(known),
        "final_ndcg10": ndcg_at([labels[j] for j in by_pri], [labels[j] for j in known]),
        "mean_grade_by_band": {b: round(sum(g) / len(g), 2) for b, g in sorted(by_band.items())},
    }
