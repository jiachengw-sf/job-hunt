"""`jobhunt calibrate`: does the qualification band predict replies in the user's own tracker?

Read-only. For each band it reports interview and offer rates and a label (separating, flat,
inverted, insufficient_data), then suggests config changes for the user to approve. It never
writes config: every weight stays where the user put it.

Rules:
- Only applications that got a reply count. An application with no reply is reported, never
  treated as evidence of poor fit, and left out of every rate.
- "Reached" uses the whole status history (append-only events), so a job that went
  Interviewing -> Rejected still counts as an interview.
- Skips with a reason (states.yml) say what the user turns down; frequent reasons among
  strong/good jobs drive preference suggestions.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from jobhunt.models import Config

BANDS = ("strong", "good", "stretch", "not_a_fit")
HIGH_BANDS = ("strong", "good")
INTERVIEW = {"OA", "Interviewing", "Offer"}
APPLIED = {"Applied", "Rejected"} | INTERVIEW

Label = Literal["separating", "flat", "inverted", "insufficient_data"]


class BandStats(BaseModel):
    band: str = Field(description="strong | good | stretch | not_a_fit | unbanded")
    applied: int = 0
    no_reply: int = Field(0, description="Applied, no reply yet. Never counted against fit.")
    replies: int = Field(0, description="Interviews plus rejections: the base for the rates.")
    interviews: int = Field(0, description="Reached OA, Interviewing or Offer.")
    rejected: int = Field(0, description="Rejected without an interview.")
    offers: int = 0
    interview_rate: float | None = Field(None, description="interviews / replies")
    offer_rate: float | None = Field(None, description="offers / replies")
    provisional: int = Field(0, description="Applied jobs whose band is still provisional.")
    skipped: int = 0
    label: Label | None = Field(None, description="None for unbanded jobs.")
    compared_with: str | None = Field(None, description="The band this label compares against.")


class Suggestion(BaseModel):
    kind: Literal["config", "review"]
    key: str | None = Field(None, description="Config key (kind=config).")
    current: float | None = None
    suggested: float | None = None
    reason: str
    job_ids: list[int] = Field(default_factory=list, description="Jobs behind this suggestion.")


class CalibrationReport(BaseModel):
    applied: int
    replies: int
    no_reply: int
    bands: list[BandStats]
    skip_reasons: dict[str, dict[str, int]] = Field(
        description="Skip reason -> band -> count (unbanded included)."
    )
    suggestions: list[Suggestion]
    config_snippet: str | None = Field(
        None,
        description="YAML for ~/.jobhunt/config.yaml with every config suggestion. Not applied.",
    )
    note: str


def _reached(conn: sqlite3.Connection) -> dict[int, set[str]]:
    seen: dict[int, set[str]] = defaultdict(set)
    for r in conn.execute("SELECT job_id, details_json FROM events WHERE kind='status_changed'"):
        d = json.loads(r["details_json"] or "{}")
        seen[r["job_id"]].update(s for s in (d.get("from"), d.get("to")) if s)
    return seen


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.job_id, a.status, a.applied_on, a.skip_reason, f.band, f.provisional "
        "FROM applications a LEFT JOIN fits f ON f.job_id = a.job_id ORDER BY a.job_id"
    ).fetchall()


def _label(stats: dict[str, BandStats], cfg: Config) -> None:
    c = cfg.calibration
    ready = [b for b in BANDS if stats[b].replies >= c.min_responses]
    for b in BANDS:
        s = stats[b]
        if b not in ready or len(ready) < 2:
            s.label = "insufficient_data"
            continue
        i = ready.index(b)
        # Compare with the next band down that has data; the lowest such band compares upward.
        upper, lower = (b, ready[i + 1]) if i + 1 < len(ready) else (ready[i - 1], b)
        s.compared_with = lower if upper == b else upper
        diff = (stats[upper].interview_rate or 0) - (stats[lower].interview_rate or 0)
        if diff > c.flat_margin:
            s.label = "separating"
        elif diff < -c.flat_margin:
            s.label = "inverted"
        else:
            s.label = "flat"


def _must_mix(conn: sqlite3.Connection, job_ids: list[int]) -> Counter[str]:
    """How the must-haves on these jobs were scored: inferred, partial, unmet or unassessed."""
    mix: Counter[str] = Counter()
    if not job_ids:
        return mix
    q = ",".join("?" * len(job_ids))
    for r in conn.execute(
        f"SELECT status, tier FROM requirements WHERE importance='must' AND job_id IN ({q})",
        job_ids,
    ):
        if r["status"] is None:
            mix["unassessed"] += 1
        elif r["status"] == "met":
            mix["inferred" if r["tier"] != "stated" else "stated"] += 1
        else:
            mix[r["status"]] += 1
    mix.pop("stated", None)
    return mix


def _band_suggestions(
    conn: sqlite3.Connection,
    stats: dict[str, BandStats],
    interviewed: dict[str, list[int]],
    cfg: Config,
) -> list[Suggestion]:
    out: list[Suggestion] = []
    step = cfg.calibration.step
    for upper in BANDS:
        s = stats[upper]
        lower = s.compared_with
        if s.label not in ("flat", "inverted") or lower is None:
            continue
        if BANDS.index(lower) < BANDS.index(upper):
            continue  # this pair is reported from the upper band's side
        lo = stats[lower]
        why = (
            f"{upper} jobs got interviews at {s.interview_rate:.0%} ({s.interviews}/{s.replies} "
            f"replies) vs {lo.interview_rate:.0%} for {lower} ({lo.interviews}/{lo.replies}): "
            f"the bands are {s.label}."
        )
        if upper != "not_a_fit":
            cur = getattr(cfg.fit.bands, upper)
            if cur + step <= 1:
                out.append(
                    Suggestion(
                        kind="config",
                        key=f"fit.bands.{upper}",
                        current=cur,
                        suggested=round(cur + step, 2),
                        reason=why + f" A higher {upper} threshold keeps only closer matches.",
                        job_ids=interviewed.get(upper, []),
                    )
                )
        # Rubric: how did the lower band's interviews score their must-haves?
        ids = interviewed.get(lower, [])
        mix = _must_mix(conn, ids)
        if not mix:
            continue
        top, n = mix.most_common(1)[0]
        q = cfg.fit.qualification
        if top == "inferred" and q.met_inferred + step < q.met_stated:
            out.append(
                Suggestion(
                    kind="config",
                    key="fit.qualification.met_inferred",
                    current=q.met_inferred,
                    suggested=round(q.met_inferred + step, 2),
                    reason=f"The {lower} jobs that led to interviews were held back mostly by "
                    f"inferred must-have matches ({n}); the rubric may credit them too little.",
                    job_ids=ids,
                )
            )
        elif top == "partial" and q.partial + step < q.met_stated:
            out.append(
                Suggestion(
                    kind="config",
                    key="fit.qualification.partial",
                    current=q.partial,
                    suggested=round(q.partial + step, 2),
                    reason=f"The {lower} jobs that led to interviews were held back mostly by "
                    f"partial must-have matches ({n}); the rubric may credit them too little.",
                    job_ids=ids,
                )
            )
        elif top == "unassessed":
            out.append(
                Suggestion(
                    kind="review",
                    reason=f"The {lower} jobs that led to interviews have {n} must-haves not "
                    "judged yet. Run `jobhunt tasks` and finish the judgment tasks first.",
                    job_ids=ids,
                )
            )
        else:
            out.append(
                Suggestion(
                    kind="review",
                    reason=f"The {lower} jobs that led to interviews have {n} must-haves scored "
                    "unmet. Check them with `jobhunt show <id>`: if you do have a skill, add the "
                    "line to your profile so code can find it.",
                    job_ids=ids,
                )
            )
    return out


SKIP_ADVICE = {
    "location": ("fit.preference.location", "Or make your locations a Must in preferences.yaml."),
    "pay": ("fit.preference.pay", "Or set a pay floor as a Must in preferences.yaml."),
    "domain": (
        "fit.preference.industry",
        "Or add the industries to avoid, or title keywords to exclude, in preferences.yaml.",
    ),
}


def _skip_suggestions(skips: dict[str, list[int]], cfg: Config) -> list[Suggestion]:
    c = cfg.calibration
    total = sum(len(v) for v in skips.values())
    out: list[Suggestion] = []
    for reason, ids in sorted(skips.items(), key=lambda kv: -len(kv[1])):
        n = len(ids)
        if n < c.skip_min or n / total < c.skip_share:
            continue
        why = f"You skipped {n} of {total} strong/good jobs for {reason!r}."
        if reason == "too_senior":
            cur = cfg.fit.level_mismatch_years
            if cur - 1 >= 1:
                out.append(
                    Suggestion(
                        kind="config",
                        key="fit.level_mismatch_years",
                        current=cur,
                        suggested=cur - 1,
                        reason=why + " A smaller gap caps more over-senior roles at stretch.",
                        job_ids=ids,
                    )
                )
            else:
                out.append(
                    Suggestion(
                        kind="review",
                        reason=why + " Add title keywords like senior or staff to exclude.",
                        job_ids=ids,
                    )
                )
        elif reason in SKIP_ADVICE:
            key, alt = SKIP_ADVICE[reason]
            cur = getattr(cfg.fit.preference, key.rsplit(".", 1)[1])
            out.append(
                Suggestion(
                    kind="config",
                    key=key,
                    current=cur,
                    suggested=round(cur + c.step, 2),
                    reason=why + f" More weight lowers the priority of jobs that miss it. {alt}",
                    job_ids=ids,
                )
            )
        else:
            out.append(
                Suggestion(
                    kind="review",
                    reason=why + " Read the Notes on these jobs for a pattern worth a Must.",
                    job_ids=ids,
                )
            )
    return out


def _snippet(suggestions: list[Suggestion]) -> str | None:
    tree: dict = {}
    for s in suggestions:
        if s.kind != "config" or s.key is None:
            continue
        *parents, leaf = s.key.split(".")
        node = tree
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = s.suggested
    return yaml.safe_dump(tree, sort_keys=False) if tree else None


def calibrate(conn: sqlite3.Connection, cfg: Config) -> CalibrationReport:
    reached = _reached(conn)
    stats = {b: BandStats(band=b) for b in (*BANDS, "unbanded")}
    interviewed: dict[str, list[int]] = defaultdict(list)
    skip_reasons: dict[str, Counter[str]] = defaultdict(Counter)
    high_skips: dict[str, list[int]] = defaultdict(list)
    for r in _rows(conn):
        band = r["band"] or "unbanded"
        s = stats[band]
        ever = reached.get(r["job_id"], set()) | {r["status"]}
        applied = bool(r["applied_on"]) or bool(ever & APPLIED)
        if r["status"] == "Skipped" and not applied:
            s.skipped += 1
            reason = r["skip_reason"] or "other"
            skip_reasons[reason][band] += 1
            if band in HIGH_BANDS:
                high_skips[reason].append(r["job_id"])
            continue
        if not applied:
            continue
        s.applied += 1
        s.provisional += bool(r["provisional"])
        if ever & INTERVIEW:
            s.interviews += 1
            s.offers += "Offer" in ever
            interviewed[band].append(r["job_id"])
        elif "Rejected" in ever:
            s.rejected += 1
        else:
            s.no_reply += 1  # still waiting, or withdrawn before any reply
    for s in stats.values():
        s.replies = s.interviews + s.rejected
        if s.replies:
            s.interview_rate = round(s.interviews / s.replies, 3)
            s.offer_rate = round(s.offers / s.replies, 3)
    _label(stats, cfg)
    suggestions = _band_suggestions(conn, stats, interviewed, cfg) + _skip_suggestions(
        high_skips, cfg
    )
    bands = [stats[b] for b in BANDS]
    if stats["unbanded"].applied or stats["unbanded"].skipped:
        bands.append(stats["unbanded"])
    applied = sum(s.applied for s in bands)
    replies = sum(s.replies for s in bands)
    return CalibrationReport(
        applied=applied,
        replies=replies,
        no_reply=sum(s.no_reply for s in bands),
        bands=bands,
        skip_reasons={k: dict(v) for k, v in sorted(skip_reasons.items())},
        suggestions=suggestions,
        config_snippet=_snippet(suggestions),
        note=(
            "Rates use replies only: an application with no reply is never evidence of poor fit. "
            f"A band needs {cfg.calibration.min_responses} replies for a label. Suggestions are "
            "for you to approve; nothing is changed until you edit config.yaml."
        ),
    )
