"""answers.json: every value the engine may type into one job's form, built by code.

Values come only from the profile and the answer bank, through the classifier's resolution. The
model never picks a value. Each field gets an action:

  fill       code types or picks the value
  recommend  shown next to the field for the user to set (Lever radios and checkboxes)
  human      left for the user on purpose (attestations, consents, unreviewed EEO, essays
             the user hasn't approved with `jobhunt essays approve`)
  skip       optional and unanswered, or hidden on the page by another answer: left blank

A field the page reveals only after another answer (``shown_if``) follows that answer: it is
skipped when the answer being filled hides it, and left to the user when the user answers it.

The validator then checks the sheet against the form spec before any browser opens: every
required field is present, each choice value is one of the form's own options, text fits its
limit, and files exist. answers.json is owner-only and never goes into an envelope.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from jobhunt.bank.keys import AnswerContext
from jobhunt.bank.match import Matcher
from jobhunt.forms import education
from jobhunt.forms.classify import Classification, FieldResult, fill_value
from jobhunt.forms.model import EducationBlock, FormField, FormSpec
from jobhunt.paths import secure_file

Action = Literal["fill", "recommend", "human", "skip"]
CHOICE = frozenset({"select", "multiselect", "radio", "checkbox", "boolean"})
# zero-width and bidi control characters never go into a form
MAX_EDUCATION = 6  # education entries a sheet may fill (the first plus "Add another" copies)
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿]")


class FillField(BaseModel):
    id: str
    label: str
    type: str
    required: bool
    section: str
    key: str | None = None
    action: Action
    value: str | list[str] | None = None
    human: list[str] = Field(default_factory=list, description="Why the user reviews this field.")
    note: str | None = None
    options: list[str] = Field(default_factory=list)
    max_length: int | None = None


class AnswerSheet(BaseModel):
    job_id: int
    ats: str
    url: str | None
    built_at: str
    captcha_at_submit: bool = False
    phone_country: str | None = Field(None, description="ISO alpha-2, set before the number.")
    resume: str | None = Field(None, description="The resume chosen for this job (its path).")
    fields: list[FillField] = Field(default_factory=list)

    def to_fill(self) -> list[FillField]:
        return [f for f in self.fields if f.action == "fill"]


class Problem(BaseModel):
    id: str
    label: str
    problem: str


def _clean(v: str | list[str] | None) -> str | list[str] | None:
    if isinstance(v, list):
        return [_INVISIBLE.sub("", x) for x in v]
    return _INVISIBLE.sub("", v) if isinstance(v, str) else v


def _action(fr: FieldResult, ats: str) -> tuple[Action, str | None]:
    if fr.resolution == "answered":
        if ats == "lever" and fr.type in ("radio", "checkbox"):
            return "recommend", "Lever: tick this yourself; the recommended value is shown"
        if any(h.startswith(("attestation", "consent")) for h in fr.human):
            return "human", "yours to answer on the form"
        return "fill", None
    if fr.resolution in ("human_only", "essay"):
        return "human", "yours to answer on the form"
    if fr.required:
        return "human", fr.reason or "no confirmed answer: yours to fill"
    return "skip", None


def _hidden(f: FormField | None, sheet: dict[str, FillField]) -> bool:
    """True when the value being filled into the revealing field keeps ``f`` hidden."""
    if not (f and f.shown_if) or (parent := sheet.get(f.shown_if.field)) is None:
        return False
    return parent.action == "fill" and parent.value not in f.shown_if.options


def _follow(ff: FillField, f: FormField | None, sheet: dict[str, FillField]) -> None:
    """Apply ``shown_if``: a field hidden by a filled answer is skipped; one revealed only by
    the user's own answer is theirs too."""
    if not (f and f.shown_if) or (parent := sheet.get(f.shown_if.field)) is None:
        return
    if _hidden(f, sheet):
        ff.action, ff.value = "skip", None
        ff.note = f"not shown: your answer to “{parent.label}” sets it"
    elif parent.action != "fill" and ff.action in ("fill", "recommend"):
        ff.action, ff.value = "human", None
        ff.note = f"appears once you answer “{parent.label}”"


def build_sheet(
    job_id: int,
    spec: FormSpec,
    cls: Classification,
    ctx: AnswerContext,
    matcher: Matcher,
    now: str,
    essays: dict[str, str] | None = None,
) -> AnswerSheet:
    """Map each form field to a value from the profile and bank (or to the user). ``essays`` holds
    the essay answers the user approved (``jobhunt essays approve``), by field id."""
    out: list[FillField] = []
    by_id = {f.id: f for f in spec.fields}
    essays = essays or {}
    for fr in cls.fields:
        action, note = _action(fr, spec.ats)
        f = by_id.get(fr.id)
        opts = f.options if f else None
        value = fill_value(fr, ctx, matcher, opts) if action in ("fill", "recommend") else None
        if fr.resolution == "essay" and fr.id in essays:
            action, value, note = "fill", essays[fr.id], "your approved essay: reread it here"
        if action == "fill" and value is None:
            action, note = ("human" if fr.required else "skip"), "no value to fill"
        ff = FillField(
            id=fr.id,
            label=fr.label,
            type=fr.type,
            required=fr.required,
            section=fr.section,
            key=fr.key,
            action=action,
            value=_clean(value),
            human=fr.human,
            note=note,
            options=f.options if f else fr.options,
            max_length=fr.max_length,
        )
        _follow(ff, f, {x.id: x for x in out})
        out.append(ff)
    if spec.education:
        out += _more_education(spec.education, ctx)
    return AnswerSheet(
        job_id=job_id,
        ats=spec.ats,
        url=spec.url,
        built_at=now,
        captcha_at_submit=spec.captcha_at_submit,
        phone_country=ctx.answers.identity.phone_country,
        resume=str(ctx.resume) if ctx.resume else None,
        fields=out,
    )


def _more_education(block: EducationBlock, ctx: AnswerContext) -> list[FillField]:
    """Each education entry after the first, for the block's "Add another" copies. They are
    optional: a part with no confident value is left blank."""
    out: list[FillField] = []
    for n, entry in enumerate(ctx.profile.education[1:MAX_EDUCATION], start=1):
        for f in education.fields(block, n):
            part = education.BY_DOM[f.id.rsplit("--", 1)[0]]
            value = education.raw_value(part, entry)
            if value and f.options:
                value = education.pick(part, value, f.options)
            out.append(
                FillField(
                    id=f.id,
                    label=f.label,
                    type=f.type,
                    required=False,
                    section=f.section,
                    action="fill" if value else "skip",
                    value=_clean(value),
                    note=None if value else "no confident value: left blank",
                    options=f.options,
                )
            )
    return out


def form_fields(spec: FormSpec) -> dict[str, FormField]:
    """Every field the form can take by id, including the education block's added entries."""
    fields = {f.id: f for f in spec.fields}
    if spec.education:
        for n in range(1, MAX_EDUCATION):
            fields.update({f.id: f for f in education.fields(spec.education, n)})
    return fields


def validate(sheet: AnswerSheet, spec: FormSpec) -> list[Problem]:
    """Check every value against the form itself. An empty list means the sheet may be filled."""
    problems: list[Problem] = []
    fields = form_fields(spec)
    by_sheet = {f.id: f for f in sheet.fields}
    sheet_ids = set(by_sheet)

    def bad(fid: str, label: str, msg: str) -> None:
        problems.append(Problem(id=fid, label=label, problem=msg))

    for f in spec.fields:
        if f.id not in sheet_ids:
            bad(f.id, f.label, "the form has this field but answers.json doesn't")
    for ff in sheet.fields:
        f = fields.get(ff.id)
        if f is None:
            bad(ff.id, ff.label, "not a field on this form (question ids differ per job)")
            continue
        if f.required and ff.action == "skip" and not _hidden(f, by_sheet):
            bad(ff.id, ff.label, "required but left blank")
        if ff.action not in ("fill", "recommend"):
            continue
        if ff.value in (None, "", []):
            bad(ff.id, ff.label, "no value")
            continue
        vals = ff.value if isinstance(ff.value, list) else [ff.value]
        if f.type == "file":
            if not Path(vals[0]).is_file():
                bad(ff.id, ff.label, "the file doesn't exist")
            continue
        if f.type in CHOICE:
            options = f.options or (["Yes", "No"] if f.type == "boolean" else [])
            if len(vals) > 1 and f.type not in ("multiselect", "checkbox"):
                bad(ff.id, ff.label, "several values for a single-choice field")
            if options:
                for v in vals:
                    if v not in options:
                        bad(ff.id, ff.label, "value isn't one of the form's options")
            continue
        if any(_INVISIBLE.search(v) for v in vals):
            bad(ff.id, ff.label, "value contains invisible characters")
        if f.max_length and any(len(v) > f.max_length for v in vals):
            bad(ff.id, ff.label, f"longer than the field's {f.max_length}-character limit")
    return problems


def write_sheet(path: Path, sheet: AnswerSheet) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sheet.model_dump(mode="json"), indent=1), encoding="utf-8")
    secure_file(tmp)
    tmp.replace(path)


def read_sheet(path: Path) -> AnswerSheet:
    return AnswerSheet.model_validate_json(path.read_text(encoding="utf-8"))
