"""After a tab is filled: highlight what needs the human, screenshot it, and report.

Highlights are outlines plus a short note next to the field (plain ``<div>``s, never form
controls). They cover empty required fields, read-back mismatches, fields the engine couldn't
find or set, required controls the form spec didn't list, recommended values left for the user
(Lever radios and checkboxes), values the ATS autofilled from the resume, and every always-human
field (attestations, consents, EEO, Ashby knockout questions). A banner says jobhunt never
submits. The report carries ids, labels and statuses only, never values.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from jobhunt.paths import secure_file
from jobhunt.prefill.adapters.base import Adapter, FieldOutcome
from jobhunt.prefill.values import AnswerSheet, FillField

TabStatus = Literal["ready_for_review", "invalid", "failed"]

_MESSAGES = {
    "mismatch": "jobhunt typed a value but the page shows something else: check it",
    "not_found": "jobhunt couldn't find this field: fill it yourself",
    "no_option": "no option matched your saved answer: pick it yourself",
    "refused": "left alone (it looked like a submit control): fill it yourself",
    "file_rejected": "this form doesn't take your resume's file type",
    "error": "jobhunt couldn't set this field: fill it yourself",
    "recommend": "tick this yourself",
    "left": "yours to answer",
    "autofilled": "filled by the ATS from your resume: check it",
    "unlisted": "required on the page but jobhunt didn't read it: fill it yourself",
}

_BANNER_JS = """(text) => {
  document.querySelectorAll('[data-jobhunt-banner]').forEach(e => e.remove());
  const b = document.createElement('div');
  b.setAttribute('data-jobhunt-banner', '1');
  b.setAttribute('role', 'status');
  b.textContent = text;
  b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;padding:8px 12px;'
    + 'background:#1f2937;color:#fff;font:14px/1.4 system-ui,sans-serif;text-align:center';
  document.body.appendChild(b);
}"""

_NOTE_JS = """(el, arg) => {
  const box = el.closest('.field-wrapper, .select__container, .file-upload, .phone-input,'
    + ' [data-field-path], li.application-question, fieldset') || el;
  box.style.outline = '3px solid ' + (arg.urgent ? '#dc2626' : '#d97706');
  box.style.outlineOffset = '3px';
  const prev = box.querySelector(':scope > [data-jobhunt-note]');
  if (prev) prev.remove();
  const n = document.createElement('div');
  n.setAttribute('data-jobhunt-note', '1');
  n.setAttribute('role', 'note');
  n.textContent = arg.text;
  n.style.cssText = 'margin:4px 0;padding:4px 8px;border-radius:4px;font:13px/1.4 system-ui,'
    + 'sans-serif;background:' + (arg.urgent ? '#fee2e2' : '#fef3c7') + ';color:#111';
  box.appendChild(n);
}"""


class ReviewItem(BaseModel):
    id: str
    label: str
    required: bool
    status: str
    note: str | None = None


class TabReview(BaseModel):
    job_id: int
    company: str = ""
    title: str = ""
    ats: str
    status: TabStatus
    filled: int = Field(0, description="Fields set and read back equal.")
    to_fill: int = Field(0, description="Fields answers.json asked the engine to set.")
    resume: str | None = Field(
        None, description="The resume file jobhunt attached (its name); None if none was."
    )
    needs_you: list[ReviewItem] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list, description="Validation problems.")
    blocked_requests: int = Field(0, description="Requests the submit guard blocked.")
    screenshot: str | None = None
    error: str | None = None


def note_text(o: FieldOutcome, *, values: bool = True) -> str:
    """The note for a field. Values appear only on the user's own page (``values=True``)."""
    parts = [_MESSAGES.get(o.status, "")] if o.status != "filled" else []
    if values and o.status == "recommend" and o.expected:
        parts.append(f"recommended: {o.expected}")
    if values and o.status == "mismatch" and o.expected:
        parts.append(f"expected: {o.expected}")
    parts += o.human
    if o.note and o.status not in ("recommend", "left", "skipped", "filled"):
        parts.append(o.note)
    text = "jobhunt: " + "; ".join(p for p in parts if p)
    return text[:400]


def highlight(page: Any, adapter: Adapter, sheet: AnswerSheet, outcomes: list[FieldOutcome]) -> int:
    """Outline and annotate every field that needs the human. Returns how many were marked."""
    by_id: dict[str, FillField] = {f.id: f for f in sheet.fields}
    marked = 0
    for o in outcomes:
        if not o.needs_you:
            continue
        ff = by_id.get(o.id)
        if ff is None and o.status != "unlisted":
            continue
        try:
            el = adapter.anchor(page, ff) if ff else _by_dom(page, o.id)
            if el is None:
                continue
            urgent = o.required and o.status not in ("filled", "recommend")
            el.evaluate(_NOTE_JS, {"text": note_text(o), "urgent": urgent})
            marked += 1
        except Exception:  # the page changed under us; the report still lists it
            continue
    page.evaluate(
        _BANNER_JS,
        "jobhunt filled this form and stopped. Review the highlighted fields, then click "
        "Submit yourself. jobhunt never submits.",
    )
    return marked


def _by_dom(page: Any, ref: str) -> Any | None:
    """A control the form spec doesn't list, by its id or name on the page."""
    q = ref.replace("\\", "\\\\").replace('"', '\\"')
    loc = page.locator(f'[id="{q}"], [name="{q}"]')
    return loc.first if loc.count() else None


def screenshot(page: Any, screens: Path, job_id: int, stamp: str) -> str | None:
    screens.mkdir(parents=True, exist_ok=True)
    path = screens / f"job-{job_id}-{re.sub(r'[^0-9A-Za-z]', '', stamp)}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception:
        return None
    secure_file(path)
    return str(path)


def review(
    job_id: int,
    sheet: AnswerSheet,
    outcomes: list[FieldOutcome],
    *,
    company: str = "",
    title: str = "",
    blocked: int = 0,
    shot: str | None = None,
) -> TabReview:
    to_fill = len(sheet.to_fill())
    filled = sum(
        1 for o in outcomes if o.status == "filled" and o.id in {f.id for f in sheet.fields}
    )
    items = [
        ReviewItem(
            id=o.id,
            label=o.label,
            required=o.required,
            status=o.status,
            note=note_text(o, values=False).removeprefix("jobhunt: ") or None,
        )
        for o in outcomes
        if o.needs_you
    ]
    items.sort(key=lambda i: (not i.required, i.status == "left"))
    by = {o.id: o for o in outcomes}
    attached = next(
        (
            Path(str(f.value)).name
            for f in sheet.fields
            if f.key == "files.resume"
            and f.value
            and getattr(by.get(f.id), "status", "") == "filled"
        ),
        None,
    )
    return TabReview(
        job_id=job_id,
        company=company,
        title=title,
        ats=sheet.ats,
        status="ready_for_review",
        filled=filled,
        to_fill=to_fill,
        resume=attached,
        needs_you=items,
        blocked_requests=blocked,
        screenshot=shot,
    )
