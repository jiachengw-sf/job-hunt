"""Ingest of ``company_research`` results: budget, quote length, and every quote re-checked on
its page.

Each cited page is fetched once (public GET, same throttle and User-Agent as discovery). A quote
missing from a page that loaded is a rejection; facts from a page that can't be loaded are dropped
from the brief and reported, never kept unverified.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import ValidationError

from jobhunt.discovery.text import html_to_text
from jobhunt.fit.profile_lines import profile_lines
from jobhunt.http import TransportError
from jobhunt.ingest.core import Ctx, register, schema_problems
from jobhunt.ingest.verify import Problem, check_line, clean_text, quote_in, word_count
from jobhunt.research import FACT_FIELDS, MAX_QUOTE_WORDS, TASK_KIND, ResearchResult


def _page_text(ctx: Ctx, url: str) -> str | None:
    try:
        resp = ctx.client.get(url, accept="text/html,application/xhtml+xml,text/plain;q=0.9")
    except TransportError:
        return None
    if not resp.ok:
        return None
    raw = resp.body.decode("utf-8", errors="replace")
    ctype = resp.headers.get("content-type", "")
    if "html" in ctype or raw.lstrip()[:1] == "<":
        return html_to_text(raw) or ""
    return raw


class CompanyResearch:
    kind = TASK_KIND

    def check(
        self, conn: sqlite3.Connection, raw: dict[str, Any], task: sqlite3.Row, ctx: Ctx
    ) -> tuple[tuple[ResearchResult, dict[str, list[str]]] | None, list[Problem]]:
        try:
            res = ResearchResult.model_validate(raw)
        except ValidationError as e:
            return None, schema_problems(e)
        cfg = ctx.inputs.cfg.research
        problems: list[Problem] = []
        if res.company.strip().lower() != (task["subject"] or "").lower():
            problems.append(Problem("$.company", f"must be {task['subject']!r}"))
        if res.subagents_used:
            problems.append(Problem("$.subagents_used", "research must not use subagents"))
        if res.searches_used > cfg.max_searches:
            problems.append(
                Problem("$.searches_used", f"over budget: at most {cfg.max_searches} searches")
            )
        if res.fetches_used > cfg.max_fetches or len(set(res.sources)) > cfg.max_fetches:
            problems.append(
                Problem("$.fetches_used", f"over budget: at most {cfg.max_fetches} fetches")
            )
        if len(set(res.sources)) > res.fetches_used:
            problems.append(Problem("$.sources", "more sources than fetches_used"))
        sources = set(res.sources)
        facts = [(f, i, fact) for f in FACT_FIELDS for i, fact in enumerate(getattr(res, f))]
        for field, i, fact in facts:
            path = f"$.{field}[{i}]"
            if fact.url not in sources:
                problems.append(Problem(f"{path}.url", "not in sources; list every fetched page"))
            n = word_count(fact.quote)
            if n > MAX_QUOTE_WORDS:
                problems.append(Problem(f"{path}.quote", f"{n} words; at most {MAX_QUOTE_WORDS}"))
            if field == "recent_news" and not fact.date:
                problems.append(Problem(f"{path}.date", "news needs a date (YYYY-MM[-DD])"))
        lines = {ln.ref: ln for ln in profile_lines(ctx.inputs.profile)}
        for i, a in enumerate(res.user_angle):
            check_line(a.evidence_ref, a.evidence_quote, lines, f"$.user_angle[{i}]", problems)
        if problems:
            return None, problems
        # every quote against its page, one fetch per page
        pages = {url: _page_text(ctx, url) for url in sorted({f.url for _, _, f in facts})}
        dropped: dict[str, list[str]] = {}
        for field, i, fact in facts:
            text = pages[fact.url]
            if text is None:
                dropped.setdefault(fact.url, []).append(f"{field}[{i}]")
            elif not quote_in(fact.quote, text):
                problems.append(
                    Problem(f"$.{field}[{i}].quote", f"not found on {fact.url}; quote it exactly")
                )
        if not problems and all(pages.get(f.url) is None for f in res.what_they_do):
            problems.append(Problem("$.what_they_do", "no cited page could be loaded to verify it"))
        return (None if problems else (res, dropped)), problems

    def apply(
        self,
        conn: sqlite3.Connection,
        parsed: tuple[ResearchResult, dict[str, list[str]]],
        task: sqlite3.Row,
        ctx: Ctx,
    ) -> dict[str, Any]:
        res, dropped = parsed
        gone = {(u, p) for u, ps in dropped.items() for p in ps}
        brief: dict[str, Any] = {}
        for field in FACT_FIELDS:
            brief[field] = [
                {
                    "text": clean_text(f.text),
                    "url": f.url,
                    "quote": clean_text(f.quote),
                    **({"date": f.date} if f.date else {}),
                }
                for i, f in enumerate(getattr(res, field))
                if (f.url, f"{field}[{i}]") not in gone
            ]
        lines = {ln.ref: ln for ln in profile_lines(ctx.inputs.profile)}
        brief["user_angle"] = [
            {
                "text": clean_text(a.text),
                "evidence_ref": a.evidence_ref,
                "evidence_quote": lines[a.evidence_ref].text,
                "fact_url": a.fact_url,
            }
            for a in res.user_angle
        ]
        brief["summary"] = brief["what_they_do"][0]["text"] if brief["what_they_do"] else ""
        brief["not_found"] = [clean_text(n) for n in res.not_found]
        brief["sources"] = res.sources
        brief["dropped_unreachable"] = dropped
        brief["budget_used"] = {"searches": res.searches_used, "fetches": res.fetches_used}
        brief["task_id"] = task["id"]
        conn.execute(
            "INSERT INTO companies (name, brief_json, brief_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET brief_json=excluded.brief_json, "
            "brief_at=excluded.brief_at",
            (task["subject"], json.dumps(brief), ctx.now),
        )
        return {
            "company": task["subject"],
            "facts": sum(len(brief[f]) for f in FACT_FIELDS),
            "dropped_unreachable": sum(len(v) for v in dropped.values()),
        }


register(CompanyResearch())
