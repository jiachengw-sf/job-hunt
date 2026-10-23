"""Greenhouse: ``GET boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}?questions=true``.

Public, no auth. Returns each question's label, ``required`` flag, field types and options, plus
the ``location_questions``, ``compliance`` (EEOC) and ``demographic_questions`` blocks.

The page builds one EEOC field the API doesn't list (verified live, 2026-10-18): from the
``race`` question it first shows "Are you Hispanic/Latino?" (``hispanic_ethnicity``). Yes sets
race to Hispanic or Latino and Decline sets it to Decline To Self Identify, both without showing
it; only No reveals the Race picker.

Education sits outside ``questions[]`` (verified live, 2026-10-23): the top-level ``education``
is ``education_required`` or ``education_optional`` (absent when hidden). Which parts show, and
which of them are required, is only on the public job page, as ``education_config``
(``school_name``, ``degree``, ``discipline``, ``start_month`` ... each ``required``, ``optional``
or ``hidden``). Without it, every part is assumed shown with the API's flag, so a job is never
called Ready on a guess. Degree and discipline options are public GETs on
``boards.greenhouse.io/v1/boards/{board}/education/{degrees,disciplines}``; schools are searched
as you type, so the school is matched on the page.
"""

from __future__ import annotations

import json
import re
import weakref
from typing import Any

from jobhunt.discovery.sources import _greenhouse_pay
from jobhunt.discovery.text import html_to_text
from jobhunt.forms import education
from jobhunt.forms.model import (
    EducationBlock,
    FieldType,
    FormError,
    FormField,
    FormSpec,
    ShownIf,
    one_line,
)
from jobhunt.http import HttpClient, TransportError

API = "https://boards-api.greenhouse.io/v1/boards"
EDU_API = "https://boards.greenhouse.io/v1/boards"
EDUCATION = {"education_required": True, "education_optional": False}
_CONFIG = re.compile(r'"education_config"\s*:\s*(\{[^{}]*\})')
_MAX_OPTION_PAGES = 10
# degree and discipline lists are per board: read once per client
_option_cache: weakref.WeakKeyDictionary[HttpClient, dict[tuple[str, str], list[str]]] = (
    weakref.WeakKeyDictionary()
)

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
    page = f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}"
    if not isinstance(data, dict) or data.get("education") not in EDUCATION:
        return parse(data, apply_url=page)
    config = read_education_config(client, page)
    shown = parts_shown(data["education"], config)
    return parse(
        data,
        apply_url=page,
        education_config=config,
        degrees=_options(client, board, "degrees") if "degree" in shown else [],
        disciplines=_options(client, board, "disciplines") if "discipline" in shown else [],
    )


def read_education_config(client: HttpClient, page_url: str) -> dict[str, str] | None:
    """The job page's ``education_config``, or None when the page can't be read."""
    try:
        resp = client.get(page_url, accept="text/html")
    except TransportError:
        return None
    if not resp.ok:
        return None
    m = _CONFIG.search(resp.body.decode("utf-8", "replace"))
    if not m:
        return None
    try:
        raw = json.loads(m.group(1))
    except ValueError:
        return None
    return {k: v for k, v in raw.items() if isinstance(v, str)}


def _options(client: HttpClient, board: str, kind: str) -> list[str]:
    """Every option of a board's degree or discipline list ([] if it can't be read: the page
    is then matched as it fills)."""
    cache = _option_cache.setdefault(client, {})
    if (board, kind) in cache:
        return cache[(board, kind)]
    out: list[str] = []
    for n in range(1, _MAX_OPTION_PAGES + 1):
        try:
            resp = client.get(f"{EDU_API}/{board}/education/{kind}?page={n}")
            data = resp.json() if resp.ok else None
        except (TransportError, ValueError):
            data = None
        if not isinstance(data, dict):
            break
        items = [str(i.get("text")) for i in data.get("items") or [] if i.get("text")]
        out += [i for i in items if i not in out]
        total = (data.get("meta") or {}).get("total_count")
        if not items or not isinstance(total, int) or len(out) >= total:
            break
    cache[(board, kind)] = out
    return out


def parts_shown(flag: str | None, config: dict[str, str] | None) -> dict[str, bool]:
    """Each part of the Education block the page shows, to whether it is required."""
    if flag not in EDUCATION:
        return {}
    if config is None:
        return {p.name: EDUCATION[flag] for p in education.PARTS}
    return {
        p.name: config[p.name] == "required"
        for p in education.PARTS
        if config.get(p.name) in ("required", "optional")
    }


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


def parse(
    data: Any,
    apply_url: str | None = None,
    *,
    education_config: dict[str, str] | None = None,
    degrees: list[str] | None = None,
    disciplines: list[str] | None = None,
) -> FormSpec:
    if not isinstance(data, dict) or "questions" not in data:
        raise FormError("Greenhouse response has no questions (was ?questions=true honored?)")
    out: list[FormField] = []
    for q in data.get("questions") or []:
        if f := _question(q):
            out.append(f)
    edu = None
    if shown := parts_shown(data.get("education"), education_config):
        edu = EducationBlock(
            parts=shown, degrees=list(degrees or []), disciplines=list(disciplines or [])
        )
        out += education.fields(edu, 0)
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
        education=edu,
        description=html_to_text(data.get("content")),
        pay=_greenhouse_pay(data.get("pay_input_ranges")),
    )
