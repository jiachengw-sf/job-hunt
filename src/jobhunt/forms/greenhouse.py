"""Greenhouse: ``GET boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}?questions=true``.

Public, no auth. Returns each question's label, ``required`` flag, field types and options, plus
the ``location_questions``, ``compliance`` (EEOC) and ``demographic_questions`` blocks.

The page builds one EEOC field the API doesn't list (verified live, 2026-10-18): from the
``race`` question it first shows "Are you Hispanic/Latino?" (``hispanic_ethnicity``). Yes sets
race to Hispanic or Latino and Decline sets it to Decline To Self Identify, both without showing
it; only No reveals the Race picker.
"""

from __future__ import annotations

from typing import Any

from jobhunt.discovery.sources import _greenhouse_pay
from jobhunt.discovery.text import html_to_text
from jobhunt.forms.model import FieldType, FormError, FormField, FormSpec, ShownIf, one_line
from jobhunt.http import HttpClient, TransportError

API = "https://boards-api.greenhouse.io/v1/boards"

HISPANIC = "hispanic_ethnicity"
HISPANIC_OPTIONS = ["Yes", "No", "Decline To Self Identify"]
_TYPES: dict[str, FieldType] = {
    "input_text": "text",
    "textarea": "textarea",
    "input_file": "file",
    "multi_value_single_select": "select",
    "multi_value_multi_select": "multiselect",
}


def url(board: str, job_id: str) -> str:
    return f"{API}/{board}/jobs/{job_id}?questions=true&pay_transparency=true"


def fetch(client: HttpClient, board: str, job_id: str) -> FormSpec:
    u = url(board, job_id)
    try:
        resp = client.get(u)
    except TransportError as e:
        raise FormError(str(e)) from e
    if resp.status == 404:
        raise FormError("Greenhouse says this job no longer exists", "not_found")
    if not resp.ok:
        raise FormError(f"Greenhouse returned HTTP {resp.status}")
    try:
        data = resp.json()
    except ValueError as e:
        raise FormError("Greenhouse did not return JSON") from e
    return parse(data, apply_url=f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}")


def _question(q: dict[str, Any], section: str = "main") -> FormField | None:
    fields = [f for f in q.get("fields") or [] if f.get("type") != "input_hidden"]
    if not fields:
        return None
    first = fields[0]
    ftype = _TYPES.get(first.get("type", ""), "text")
    options = [str(v.get("label")) for v in first.get("values") or [] if v.get("label")]
    name = str(first.get("name") or "")
    if name == "location":
        ftype = "location"
    return FormField(
        id=name,
        label=one_line(q.get("label")) or name,
        description=html_to_text(q.get("description")) or None,
        type=ftype,
        required=bool(q.get("required")),
        options=options,
        section=section,  # type: ignore[arg-type]
    )


def _hispanic(race: FormField) -> FormField:
    """The question the page shows before Race, with Race's own required flag."""
    return FormField(
        id=HISPANIC,
        label="Are you Hispanic/Latino?",
        type="select",
        required=race.required,
        options=list(HISPANIC_OPTIONS),
        section="eeo",
    )


def parse(data: Any, apply_url: str | None = None) -> FormSpec:
    if not isinstance(data, dict) or "questions" not in data:
        raise FormError("Greenhouse response has no questions (was ?questions=true honored?)")
    out: list[FormField] = []
    for q in data.get("questions") or []:
        if f := _question(q):
            out.append(f)
    for q in data.get("location_questions") or []:
        if f := _question(q):
            out.append(f)
    for block in data.get("compliance") or []:
        for q in block.get("questions") or []:
            if f := _question(q, "eeo"):
                if f.id == "race":
                    out.append(_hispanic(f))
                    f.shown_if = ShownIf(field=HISPANIC, options=["No"])
                out.append(f)
    demo = data.get("demographic_questions") or {}
    for q in demo.get("questions") or []:
        opts = [str(o.get("label")) for o in q.get("answer_options") or [] if o.get("label")]
        out.append(
            FormField(
                id=f"demographic_{q.get('id')}",
                label=one_line(q.get("label")) or "Demographic question",
                type="multiselect" if q.get("type") == "multi_value_multi_select" else "select",
                required=bool(q.get("required")),
                options=opts,
                section="eeo",
            )
        )
    for dc in data.get("data_compliance") or []:
        if dc.get("requires_consent") or dc.get("requires_processing_consent"):
            out.append(
                FormField(
                    id=f"data_compliance_{dc.get('type')}",
                    label=f"Consent to data processing ({str(dc.get('type') or '').upper()})",
                    type="checkbox",
                    required=True,
                    section="consent",
                )
            )
    return FormSpec(
        ats="greenhouse",
        url=apply_url or data.get("absolute_url"),
        fields=out,
        description=html_to_text(data.get("content")),
        pay=_greenhouse_pay(data.get("pay_input_ranges")),
    )
