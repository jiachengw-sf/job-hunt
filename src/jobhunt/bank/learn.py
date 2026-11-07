"""Wordings learned from what the user reviewed: a question filled from a *similar* known
question (``forms.fill_similar``), or mapped by the AI agent (``jobhunt answers map``), becomes a
confirmed wording of that key once the user submits the application with it. The next form that
words it the same way is then an exact match, and nobody asks again.

Only the label goes into answers.yaml (with the company's name as "[Company]"), never a value.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jobhunt.bank.keys import BUILTIN
from jobhunt.bank.match import company_template, normalize
from jobhunt.config import read_yaml
from jobhunt.locking import file_lock
from jobhunt.models import Answers
from jobhunt.onboarding.form import write_yaml
from jobhunt.paths import Paths

log = logging.getLogger(__name__)


def add_wordings(raw: dict[str, Any], pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Add (label, key) pairs to answers.yaml's bank as confirmed wordings. Returns those added."""
    bank = raw.setdefault("bank", [])
    added: list[tuple[str, str]] = []
    for label, key in pairs:
        spec = BUILTIN.get(key)
        e = next((x for x in bank if x.get("key") == key), None)
        if e is None:
            if spec is None:
                continue  # a custom key that's gone: nothing to attach it to
            e = {"key": key, "question": spec.question, "wordings": []}
            bank.append(e)
        have = {normalize(w) for w in e.setdefault("wordings", [])}
        # an agent-proposed wording the user has now submitted with is confirmed
        e["proposed"] = [w for w in e.get("proposed") or [] if normalize(w) != normalize(label)]
        if not e["proposed"]:
            e.pop("proposed")
        if normalize(label) in have or not normalize(label):
            continue
        e["wordings"].append(label)
        added.append((label, key))
    return added


def reviewed_pairs(sheets: list[Any], company: str) -> list[tuple[str, str]]:
    """(label, key) for every field filled from a similar or agent-mapped question."""
    out: list[tuple[str, str]] = []
    for sheet in sheets:
        for f in sheet.fields:
            if f.similar and f.key and f.action == "fill":
                out.append((company_template(f.label, company), f.key))
    return out


def learn_from_submission(paths: Paths, sheet_files: list[Path], company: str) -> list[str]:
    """The user submitted with these answers: learn the similar wordings they reviewed. Best
    effort: never fails the caller. Returns the keys that gained a wording."""
    from jobhunt.prefill.values import read_sheet

    sheets = []
    for p in sheet_files:
        try:
            sheets.append(read_sheet(p))
        except Exception:
            continue
    pairs = reviewed_pairs(sheets, company)
    if not pairs or not paths.answers.exists():
        return []
    try:
        with file_lock(paths.lock, timeout=60):
            raw = read_yaml(paths.answers) or {}
            added = add_wordings(raw, pairs)
            if added:
                Answers.model_validate(raw)
                write_yaml(paths.answers, raw, "Your screening answers. Sensitive: never share.")
    except Exception as e:
        log.warning("couldn't learn reviewed wordings: %s", e)
        return []
    return sorted({k for _, k in added})
