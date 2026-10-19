"""The digest: what changed since the last one, and what's waiting on the user.

Sections: new Ready jobs, questions only the user can answer, Essay jobs with prompts and
deadlines, Manual jobs (hand-off packets), closures, legitimacy flags, follow-ups due, referral
questions, and pending AI tasks. Fast alerts are separate: one push per high-priority new posting.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from jobhunt.drafts.essays import essay_prompts
from jobhunt.drafts.outreach import due_followups, referral_candidates
from jobhunt.models import Config
from jobhunt.notify.channels import Channel, NotifyError

SHOWN = 8
EPOCH = "1970-01-01T00:00:00Z"


class Item(BaseModel):
    job_id: int | None = None
    text: str
    url: str | None = None


class Digest(BaseModel):
    since: str
    until: str
    sections: dict[str, list[Item]] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    text: str = ""

    @property
    def empty(self) -> bool:
        return not any(self.counts.values())


def last_digest(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT max(at) FROM notifications WHERE kind='digest' AND status='sent'"
    ).fetchone()
    return row[0] or EPOCH


def _job_line(r: sqlite3.Row) -> str:
    band = f", {r['band']}" if r["band"] else ""
    prio = f", priority {r['priority']:.2f}" if r["priority"] is not None else ""
    return f"{r['company']} — {r['title']}{band}{prio}"


_JOBS = (
    "SELECT j.id, j.company, j.title, j.canonical_url, j.deadline, j.first_seen, f.band, "
    "f.priority FROM jobs j LEFT JOIN fits f ON f.job_id=j.id "
    "LEFT JOIN form_specs fs ON fs.job_id=j.id LEFT JOIN applications a ON a.job_id=j.id "
    "WHERE j.live=1 AND coalesce(f.gate_status,'') != 'failed' "
    "AND coalesce(a.status,'New') IN ('New','Prefilled') "
)


def _new_ids(conn: sqlite3.Connection, since: str) -> set[int]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT job_id FROM events WHERE kind='appeared' AND at > ? AND "
            "coalesce(json_extract(details_json, '$.baseline'), 0) = 0",
            (since,),
        )
    }


def build_digest(
    conn: sqlite3.Connection, cfg: Config, questions: list[dict[str, Any]], *, since: str, now: str
) -> Digest:
    today = date.fromisoformat(now[:10])
    d = Digest(since=since, until=now)
    new = _new_ids(conn, since)
    s: dict[str, list[Item]] = {}

    ready = conn.execute(_JOBS + "AND fs.class='ready' ORDER BY f.priority DESC").fetchall()
    s["ready"] = [
        Item(job_id=r["id"], text=("NEW " if r["id"] in new else "") + _job_line(r),
             url=r["canonical_url"])
        for r in ready
    ]  # fmt: skip
    s["needs_answers"] = [
        Item(text=f"{q['label']} (on {q.get('jobs') or 1} form(s))")
        for q in questions
        if q.get("required_in")
    ]
    essays = conn.execute(_JOBS + "AND fs.class='essay' ORDER BY f.priority DESC").fetchall()
    s["essays"] = []
    for r in essays:
        prompts = [p.label for p in essay_prompts(conn, r["id"]) if p.required]
        due = f" — due {r['deadline'][:10]}" if r["deadline"] else ""
        s["essays"].append(
            Item(job_id=r["id"], text=f"{_job_line(r)}{due}: " + " | ".join(prompts),
                 url=r["canonical_url"])
        )  # fmt: skip
    manual = conn.execute(
        _JOBS + "AND fs.class='manual' AND f.priority >= ? ORDER BY f.priority DESC",
        (cfg.fit.priority_threshold,),
    ).fetchall()
    s["manual"] = [
        Item(job_id=r["id"], text=f"{_job_line(r)} — `jobhunt packet {r['id']}`",
             url=r["canonical_url"])
        for r in manual
    ]  # fmt: skip
    closed = conn.execute(
        "SELECT j.id, j.company, j.title, j.canonical_url, f.band, f.priority FROM jobs j "
        "LEFT JOIN fits f ON f.job_id=j.id WHERE j.closed_at > ? AND "
        "coalesce(f.gate_status,'') != 'failed'",
        (since,),
    ).fetchall()
    s["closed"] = [Item(job_id=r["id"], text=_job_line(r)) for r in closed]
    flags = conn.execute(
        "SELECT s.job_id, s.kind, j.company, j.title FROM signals s JOIN jobs j ON j.id=s.job_id "
        "WHERE s.at > ? ORDER BY s.job_id",
        (since,),
    ).fetchall()
    s["flags"] = [
        Item(job_id=r["job_id"], text=f"{r['company']} — {r['title']}: {r['kind']}") for r in flags
    ]
    s["followups"] = [
        Item(job_id=f["job_id"], text=f"{f['company']} — {f['title']}: applied "
             f"{f['applied_on']}, follow-up {f['followups_sent'] + 1} due")
        for f in due_followups(conn, cfg.followups, today)
    ]  # fmt: skip
    s["referrals"] = [
        Item(job_id=r["job_id"], text=f"{r['question']} ({r['title']})")
        for r in referral_candidates(conn, max(cfg.fit.priority_threshold, 0.6), limit=3)
    ]
    pending = conn.execute(
        "SELECT kind, count(*) FROM tasks WHERE status='pending' GROUP BY kind"
    ).fetchall()
    names = {"requirement_evidence": "fit judgment", "company_research": "company research",
             "essay_draft": "essay draft", "essay_review": "essay review",
             "outreach_draft": "outreach draft"}  # fmt: skip
    s["tasks"] = [Item(text=f"{n} {names.get(k, k)} task(s)") for k, n in pending]

    d.counts = {k: len(v) for k, v in s.items()}
    d.counts["new_ready"] = sum(1 for r in ready if r["id"] in new)
    d.sections = {k: v[:SHOWN] for k, v in s.items()}
    d.text = render_text(d)
    return d


TITLES = {
    "ready": "Ready to pre-fill",
    "needs_answers": "Questions only you can answer",
    "essays": "Essay jobs",
    "manual": "Apply by hand",
    "closed": "Closed since last digest",
    "flags": "Legitimacy flags",
    "followups": "Follow-ups due (drafts only; you send them)",
    "referrals": "Referrals",
    "tasks": "Waiting for your AI tool",
}


def render_text(d: Digest) -> str:
    if d.empty:
        return "jobhunt: nothing new."
    lines = [f"jobhunt digest — {d.counts.get('new_ready', 0)} new Ready job(s)"]
    for key, title in TITLES.items():
        items = d.sections.get(key) or []
        if not items:
            continue
        extra = d.counts[key] - len(items)
        lines.append(f"\n{title} ({d.counts[key]}):")
        for it in items:
            lines.append(f"- {it.text}" + (f"\n  {it.url}" if it.url else ""))
        if extra > 0:
            lines.append(f"- … and {extra} more (`jobhunt jobs`)")
    lines.append('\nOpen your AI tool and say "run my job hunt" to continue.')
    return "\n".join(lines)


def digest_due(conn: sqlite3.Connection, policy: str, now: str) -> bool:
    if policy == "off":
        return False
    if policy == "every_run":
        return True
    last = last_digest(conn)
    if last == EPOCH:
        return True
    gap = datetime.fromisoformat(now.replace("Z", "+00:00")) - datetime.fromisoformat(
        last.replace("Z", "+00:00")
    )
    return gap >= timedelta(hours=20)


def record(
    conn: sqlite3.Connection, kind: str, channel: str, now: str, *, job_id: int | None = None,
    error: str | None = None,
) -> None:  # fmt: skip
    conn.execute(
        "INSERT INTO notifications (kind, job_id, channel, status, error, at) VALUES (?,?,?,?,?,?)",
        (kind, job_id, channel, "failed" if error else "sent", error, now),
    )


def push(
    conn: sqlite3.Connection, channels: list[Channel], kind: str, title: str, text: str, now: str,
    *, job_id: int | None = None, urgent: bool = False,
) -> dict[str, str]:  # fmt: skip
    """Send to every channel; record each attempt. Returns channel → "sent" or the error."""
    out = {}
    for ch in channels:
        try:
            ch.send(title, text, urgent=urgent)
        except NotifyError as e:
            record(conn, kind, ch.name, now, job_id=job_id, error=str(e))
            out[ch.name] = f"failed: {e}"
            continue
        record(conn, kind, ch.name, now, job_id=job_id)
        out[ch.name] = "sent"
    return out


def fast_alerts(
    conn: sqlite3.Connection, channels: list[Channel], cfg: Config, new_ids: list[int], now: str
) -> list[dict[str, Any]]:
    """One urgent push per new (non-baseline) posting at or above alert_priority, once per job."""
    if not cfg.notifications.alerts or not channels or not new_ids:
        return []
    sent = []
    rows = conn.execute(
        f"SELECT j.id, j.company, j.title, j.canonical_url, f.band, f.priority FROM jobs j "
        f"JOIN fits f ON f.job_id=j.id WHERE j.id IN ({','.join('?' * len(new_ids))}) "
        f"AND f.priority >= ? AND coalesce(f.gate_status,'') != 'failed' ORDER BY f.priority DESC",
        (*new_ids, cfg.notifications.alert_priority),
    ).fetchall()
    for r in rows:
        if conn.execute(
            "SELECT 1 FROM notifications WHERE kind='alert' AND job_id=? AND status='sent'",
            (r["id"],),
        ).fetchone():
            continue
        text = _job_line(r) + (f"\n{r['canonical_url']}" if r["canonical_url"] else "")
        res = push(conn, channels, "alert", f"New: {r['company']}", text, now, job_id=r["id"],
                   urgent=True)  # fmt: skip
        sent.append({"job_id": r["id"], "channels": res})
    return sent


def summary(d: Digest) -> dict[str, Any]:
    return {"since": d.since, "counts": d.counts}


def new_since(conn: sqlite3.Connection, at: str) -> list[int]:
    """Non-baseline jobs that appeared at or after ``at`` (one run's new postings)."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT job_id FROM events WHERE kind='appeared' AND at >= ? AND "
            "coalesce(json_extract(details_json, '$.baseline'), 0) = 0",
            (at,),
        )
    ]


def after_run(
    conn: sqlite3.Connection,
    cfg: Config,
    channels: list[Channel],
    questions: list[dict[str, Any]],
    *,
    run_started: str,
    now: str,
    dry_run: bool,
) -> dict[str, Any]:
    """The notify step of `jobhunt run`: fast alerts, then the digest when it's due."""
    d = build_digest(conn, cfg, questions, since=last_digest(conn), now=now)
    out: dict[str, Any] = {"digest": summary(d), "alerts": [], "digest_pushed": {}}
    if dry_run or not channels:
        out["channels"] = [c.name for c in channels]
        return out
    out["alerts"] = fast_alerts(conn, channels, cfg, new_since(conn, run_started), now)
    if not d.empty and digest_due(conn, cfg.notifications.digest, now):
        out["digest_pushed"] = push(conn, channels, "digest", "jobhunt digest", d.text, now)
    out["channels"] = [c.name for c in channels]
    return out
