"""Lever: the postings API hides custom questions, so read the hosted ``/apply`` page.

The page is server-rendered: every question sits in an ``.application-question`` element, and
each custom card also carries its definition as JSON in a hidden ``cards[<id>][baseTemplate]``
input (types, options, required). This module only GETs the page and parses the HTML. It never
loads scripts, clicks or posts anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from jobhunt.discovery.sources import LeverSource, SourceError
from jobhunt.forms.model import FieldType, FormError, FormField, FormSpec, one_line
from jobhunt.http import HttpClient, TransportError

_VOID = {"input", "br", "img", "hr", "meta", "link", "source", "area", "col", "wbr", "base"}

_CARD_TYPES: dict[str, FieldType] = {
    "text": "text",
    "textarea": "textarea",
    "multiple-choice": "radio",
    "multiple-select": "checkbox",
    "dropdown": "select",
    "file-upload": "file",
    "date": "date",
}


def apply_url(site: str, job_id: str, region: str | None = None) -> str:
    base = "https://jobs.eu.lever.co" if region == "eu" else "https://jobs.lever.co"
    return f"{base}/{site}/{job_id}/apply"


@dataclass
class _Q:
    depth: int
    label: list[str] = field(default_factory=list)
    required: bool = False
    inputs: list[dict[str, Any]] = field(default_factory=list)
    choice_labels: list[str] = field(default_factory=list)


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, set[str]]] = []
        self.questions: list[_Q] = []
        self.q: _Q | None = None
        self.in_label = 0  # depth of the .application-label element, 0 = outside
        self.in_option: dict[str, Any] | None = None
        self.in_answer = False
        self.templates: dict[str, dict[str, Any]] = {}
        self.in_form = False
        self.captcha = False

    def handle_starttag(self, tag: str, attrs_l: list[tuple[str, str | None]]) -> None:
        attrs = {k: v or "" for k, v in attrs_l}
        classes = set(attrs.get("class", "").split())
        if tag == "form":
            self.in_form = True
        if (
            "h-captcha" in classes
            or "g-recaptcha" in classes
            or attrs.get("name", "").startswith(("h-captcha", "g-recaptcha"))
        ):
            self.captcha = True
        if tag == "input" and attrs.get("name", "").endswith("[baseTemplate]"):
            try:
                tpl = json.loads(attrs.get("value") or "{}")
                card = attrs["name"].split("[", 1)[1].split("]", 1)[0]
                self.templates[card] = tpl
            except (ValueError, IndexError):
                pass
            return
        if tag not in _VOID:
            self.stack.append((tag, classes))
        depth = len(self.stack)
        if self.in_form and "application-question" in classes and tag not in _VOID:
            self.q = _Q(depth=depth)
            self.questions.append(self.q)
            return
        q = self.q
        if q is None:
            return
        if "application-label" in classes and not self.in_label:
            self.in_label = depth
        if "required" in classes and self.in_label:
            q.required = True
        if "application-answer-alternative" in classes:
            self.in_answer = True
        if tag in ("input", "textarea", "select"):
            itype = attrs.get("type", tag)
            if itype in ("hidden", "submit", "button"):
                return
            entry = {
                "tag": tag,
                "type": itype,
                "name": attrs.get("name", ""),
                "required": "required" in attrs,
                "maxlength": attrs.get("maxlength"),
                "value": attrs.get("value"),
                "options": [],
            }
            q.inputs.append(entry)
            if tag == "select":
                self.in_option = entry
        elif tag == "option" and self.in_option is not None:
            self.in_option["_opt"] = attrs.get("value", "")
            self.in_option["options"].append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.in_form = False
        if tag in _VOID:
            return
        # tolerate unclosed elements: pop to the matching tag
        while self.stack:
            t, _ = self.stack.pop()
            depth = len(self.stack) + 1
            if self.in_label and depth <= self.in_label:
                self.in_label = 0
            if self.q is not None and depth <= self.q.depth:
                self.q = None
            if t == tag:
                break
        if tag == "select":
            self.in_option = None
        if tag == "span":
            self.in_answer = False

    def handle_data(self, data: str) -> None:
        if self.q is None:
            return
        if self.in_label:
            self.q.label.append(data)
        elif self.in_option is not None and self.in_option["options"]:
            self.in_option["options"][-1] += data
        elif self.in_answer:
            self.q.choice_labels.append(data.strip())


def _field_type(entry: dict[str, Any]) -> FieldType:
    if entry["tag"] == "textarea":
        return "textarea"
    if entry["tag"] == "select":
        return "select"
    t = entry["type"]
    return {
        "file": "file",
        "email": "email",
        "tel": "phone",
        "radio": "radio",
        "checkbox": "checkbox",
        "date": "date",
        "number": "number",
        "url": "url",
    }.get(t, "text")


def _clean_label(parts: list[str]) -> str:
    return re.sub(r"\s*✱\s*", " ", " ".join(" ".join(parts).split())).strip()


def parse(page: str, url: str | None = None) -> FormSpec:
    if "application-form" not in page and "application-question" not in page:
        raise FormError("Lever page has no application form", "error")
    p = _Parser()
    p.feed(page)
    p.close()
    fields: list[FormField] = []
    seen: set[str] = set()
    for q in p.questions:
        named = [e for e in q.inputs if e["name"]]
        if not named:
            continue
        first = named[0]
        name = first["name"]
        if name in seen:
            continue
        seen.add(name)
        label = _clean_label(q.label) or name
        ftype = _field_type(first)
        options = [o.strip() for o in first["options"] if o.strip()]
        if ftype in ("radio", "checkbox"):
            options = [e["value"] for e in named if e.get("value")] or [
                c for c in q.choice_labels if c
            ]
        if name == "location":
            ftype = "location"
        required = q.required or any(e["required"] for e in named)
        max_len = next(
            (int(e["maxlength"]) for e in named if (e["maxlength"] or "").isdigit()), None
        )
        section = "eeo" if name.startswith("eeo[") else "main"
        if name.startswith("surveysResponses"):
            section = "survey"
        fields.append(
            FormField(
                id=name,
                label=label,
                type=ftype,
                required=required,
                options=options,
                max_length=max_len,
                section=section,  # type: ignore[arg-type]
            )
        )
    # card templates are authoritative for custom questions (types, options, required)
    by_id = {f.id: f for f in fields}
    for card_id, tpl in p.templates.items():
        for i, cf in enumerate(tpl.get("fields") or []):
            fid = f"cards[{card_id}][field{i}]"
            ftype = _CARD_TYPES.get(str(cf.get("type")), "text")
            opts = [str(o.get("text")) for o in cf.get("options") or [] if o.get("text")]
            existing = by_id.get(fid)
            if existing is None:
                existing = FormField(id=fid, label=one_line(cf.get("text")) or fid, type=ftype)
                fields.append(existing)
                by_id[fid] = existing
            existing.type = ftype
            existing.required = bool(cf.get("required"))
            if opts:
                existing.options = opts
            if cf.get("description"):
                existing.description = one_line(cf.get("description"))
    if not fields:
        raise FormError("Lever page has no readable fields")
    return FormSpec(ats="lever", url=url, fields=fields, captcha_at_submit=p.captcha)


def fetch(client: HttpClient, site: str, job_id: str, region: str | None = None) -> FormSpec:
    u = apply_url(site, job_id, region)
    try:
        resp = client.get(u, accept="text/html")
    except TransportError as e:
        raise FormError(str(e)) from e
    if resp.status == 404:
        raise FormError("Lever says this posting no longer exists", "not_found")
    if not resp.ok:
        raise FormError(f"Lever returned HTTP {resp.status}")
    page = resp.body.decode("utf-8", "replace")
    if re.search(r"<input[^>]+type=\"password\"", page):
        raise FormError("Lever asks for a login before the form", "error")
    spec = parse(page, u)
    try:
        posting = LeverSource.fetch_job(client, site, job_id, "view", region)
    except SourceError:
        posting = None
    if posting is not None:
        spec.description, spec.pay = posting.description, posting.pay
    return spec
