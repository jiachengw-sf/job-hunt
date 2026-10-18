"""Grounding for drafted text: every sentence cites a source, and code checks it can stand on it.

Sources are the de-identified profile lines, the user's base answers (bio, why this field, a
project), the posting (with a verbatim quote) and verified company-brief facts. A sentence may not
introduce a number, a skill, or a leadership claim its source doesn't contain. A sentence with no
source is connective tissue and may make none of those claims.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from pydantic import BaseModel, Field

from jobhunt.fit.profile_lines import ProfileLine, _redactor
from jobhunt.fit.requirements import find_skills
from jobhunt.ingest.verify import Problem, clean_text, norm, quote_in
from jobhunt.models import Answers, Profile

_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_LEAD = {
    "lead": r"lead|leads|leading|led|leader|leadership",
    "manage": r"manage|manages|managed|managing|manager",
    "found": r"founded|founder|co-?founded|co-?founder",
    "mentor": r"mentor|mentors|mentored|mentoring",
    "supervise": r"supervise|supervised|supervising|supervisor",
    "direct": r"directed|director",
    "head": r"headed",
    "president": r"president|vice president",
    "captain": r"captain|captained",
    "chair": r"chair|chaired|chairperson",
}
_LEAD_RX = {k: re.compile(rf"(?<![\w-])(?:{v})(?![\w-])", re.I) for k, v in _LEAD.items()}


def numbers(text: str) -> set[str]:
    return {m.replace(",", "") for m in _NUM.findall(text)}


def leadership(text: str) -> set[str]:
    return {k for k, rx in _LEAD_RX.items() if rx.search(text)}


@dataclass
class Source:
    ref: str
    text: str


@dataclass
class Sources:
    """What a draft may cite, keyed by ref."""

    by_ref: dict[str, Source]
    posting: str

    def lines_json(self) -> list[dict[str, str]]:
        return [{"ref": s.ref, "text": s.text} for s in self.by_ref.values()]


def base_answer_lines(profile: Profile, answers: Answers) -> list[ProfileLine]:
    rx = _redactor(profile)
    out = []
    for name in ("bio", "why_field", "project"):
        v = getattr(answers.base_answers, name)
        if v and v.strip():
            text = re.sub(r"\s+", " ", v).strip()
            out.append(
                ProfileLine(
                    f"base_answers.{name}", rx.sub("[school]", text) if rx else text, "base"
                )
            )
    return out


def brief_lines(conn: sqlite3.Connection, company: str) -> list[Source]:
    row = conn.execute(
        "SELECT brief_json FROM companies WHERE lower(name)=lower(?)", (company,)
    ).fetchone()
    if not row or not row["brief_json"]:
        return []
    brief = json.loads(row["brief_json"])
    out = []
    for field, facts in brief.items():
        if not isinstance(facts, list) or field in ("sources", "not_found", "user_angle"):
            continue
        for i, f in enumerate(facts):
            if isinstance(f, dict) and "quote" in f:
                out.append(Source(f"brief.{field}[{i}]", f"{f['text']} ({f['quote']})"))
    return out


def build_sources(
    lines: list[ProfileLine], extra: list[ProfileLine], brief: list[Source], posting: str
) -> Sources:
    by_ref = {ln.ref: Source(ln.ref, ln.text) for ln in [*lines, *extra]}
    by_ref.update({b.ref: b for b in brief})
    return Sources(by_ref, posting)


def job_sources(
    conn: sqlite3.Connection, job_text: str, company: str, profile: Profile, answers: Answers
) -> Sources:
    """Everything a draft for this job may cite."""
    from jobhunt.fit.profile_lines import profile_lines

    return build_sources(
        profile_lines(profile),
        base_answer_lines(profile, answers),
        brief_lines(conn, company),
        job_text,
    )


class Sentence(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    source: str | None = Field(
        None,
        description="A profile/base_answers/brief ref, or 'posting'. Null only for connective "
        "sentences that state no fact (no numbers, skills or roles).",
    )
    quote: str | None = Field(None, description="For source 'posting': the exact posting text.")
    new_paragraph: bool = False


def source_text(s: Sentence, sources: Sources) -> str | None:
    if s.source == "posting":
        return s.quote if s.quote and quote_in(s.quote, sources.posting) else None
    src = sources.by_ref.get(s.source or "")
    return src.text if src else None


def _claims(text: str, allowed: str, posting: str) -> list[str]:
    """Numbers, skills and leadership words in ``text`` that ``allowed`` doesn't back."""
    out = []
    extra_nums = numbers(text) - numbers(allowed)
    if extra_nums:
        out.append(f"number(s) {', '.join(sorted(extra_nums))}")
    have = set(find_skills(allowed)) | set(find_skills(posting))
    extra_skills = [k for k in find_skills(text) if k not in have]
    if extra_skills:
        out.append(f"skill(s) {', '.join(extra_skills)}")
    extra_lead = leadership(text) - leadership(allowed)
    if extra_lead:
        out.append(f"a leadership claim ({', '.join(sorted(extra_lead))})")
    return out


def check_sentences(
    sentences: list[Sentence], sources: Sources, path: str, problems: list[Problem]
) -> None:
    for i, s in enumerate(sentences):
        p = f"{path}[{i}]"
        if s.source is None:
            bad = _claims(s.text, "", "")
            if bad:
                problems.append(
                    Problem(f"{p}.source", f"states {'; '.join(bad)}: cite the line it comes from")
                )
            continue
        if s.source == "posting":
            if not s.quote or not quote_in(s.quote, sources.posting):
                problems.append(Problem(f"{p}.quote", "quote the posting exactly"))
                continue
        elif s.source not in sources.by_ref:
            problems.append(Problem(f"{p}.source", f"{s.source!r} is not a source ref"))
            continue
        allowed = source_text(s, sources) or ""
        posting = sources.posting if s.source == "posting" else ""
        bad = _claims(s.text, allowed, posting)
        if bad:
            problems.append(
                Problem(f"{p}.text", f"adds {'; '.join(bad)} not in {s.source}; don't invent them")
            )


def join_sentences(sentences: list[Sentence]) -> str:
    parts: list[str] = []
    for s in sentences:
        text = clean_text(s.text).strip()
        if parts and s.new_paragraph:
            parts.append("\n\n" + text)
        elif parts:
            parts.append(" " + text)
        else:
            parts.append(text)
    return "".join(parts)


def edit_adds_claims(old: str, new: str, cited: str, posting: str) -> list[str]:
    """What a reviewer's replacement adds that neither the replaced text nor the cited sources
    back."""
    return _claims(new, f"{old}\n{cited}", posting)


def edit_ratio(draft: str, final: str) -> float:
    """How much the user changed a draft: 0 = unchanged, 1 = rewritten."""
    from difflib import SequenceMatcher

    a, b = norm(draft), norm(final)
    if not a and not b:
        return 0.0
    return round(1 - SequenceMatcher(None, a, b, autojunk=False).ratio(), 3)
