"""Resolve every form field against the profile and answer bank, then class the job.

Classes (in order of precedence):
  manual         the form can't be read or used without the human (unsupported ATS, login, ...)
  essay          a required free-text answer beyond short facts, or a required cover letter
  needs_answers  a required short field with no confident answer (or an option that doesn't fit)
  ready          every required field maps to the profile or a confirmed bank entry

Always-human fields are highlighted in every class: attestations, consents, EEO blocks the user
hasn't reviewed, Ashby knockout questions (Ashby can auto-reject on them) and Lever's radios and
checkboxes (left for the user to tick, with the recommended value shown).

Output never carries answer values: only where a value comes from and whether it fits.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from jobhunt.bank.keys import BUILTIN, DECLINE_ALIASES, AnswerContext, is_sensitive_label
from jobhunt.bank.match import Match, Matcher, choose_option, normalize
from jobhunt.forms.model import FormField, FormSpec
from jobhunt.models.answers import DECLINE_EEO

AppClass = Literal["ready", "needs_answers", "essay", "manual"]
Resolution = Literal[
    "answered",  # a confident value that fits the field
    "unanswered",  # a known question the user hasn't answered yet
    "candidate",  # similar to a known question: the user confirms the wording once
    "unknown",  # no confident match
    "option_mismatch",  # the answer doesn't match any of the form's options
    "too_long",  # the answer exceeds the field's character limit
    "essay",  # free text the user writes (with the essay skill)
    "human_only",  # left for the user on purpose (attestation, consent, EEO not reviewed)
    "skipped",  # optional and not answered: left blank
]
BLOCKING: frozenset[str] = frozenset(
    {"unanswered", "candidate", "unknown", "option_mismatch", "too_long"}
)

_ATTEST = re.compile(
    r"\b(certify|attest|acknowledge|affirm|truthful|true and (?:accurate|complete)|accurate and "
    r"complete|ai policy|use of ai|artificial intelligence|ai (?:tools|assistance|notetaker)|"
    r"interviewed (?:here|with (?:us|company)|at company)|previously applied|privacy (?:policy|"
    r"notice)|terms and conditions|signature|e-?sign)",
    re.I,
)
_ESSAY_LABEL = re.compile(
    r"^(why|describe|tell us|explain|what (?:interests|excites|motivates|draws)|share|please "
    r"(?:describe|explain|elaborate|share|tell))|\b(elaborate|in \d+ words|cover letter)\b",
    re.I,
)
_DECLINE_RE = re.compile(
    r"\b(decline|don t wish|do not wish|prefer not|choose not|not to answer)\b"
)


class FieldResult(BaseModel):
    id: str
    label: str
    type: str
    required: bool
    section: str
    key: str | None = None
    match: str = Field(description="exact | confirmed | candidate | unknown")
    via: str = ""
    score: float = 0.0
    resolution: Resolution
    reason: str | None = None
    human: list[str] = Field(default_factory=list, description="Why you review this field.")
    sensitive: bool = False
    max_length: int | None = None
    options: list[str] = Field(default_factory=list)
    description: str | None = None


class Classification(BaseModel):
    cls: AppClass = Field(alias="class")
    reasons: list[str] = Field(default_factory=list)
    fields: list[FieldResult] = Field(default_factory=list)
    captcha_at_submit: bool = False

    model_config = {"populate_by_name": True}

    @property
    def required(self) -> list[FieldResult]:
        return [f for f in self.fields if f.required]

    @property
    def needs(self) -> list[FieldResult]:
        return [f for f in self.fields if f.required and f.resolution in BLOCKING]

    @property
    def essays(self) -> list[FieldResult]:
        return [f for f in self.fields if f.resolution == "essay"]

    @property
    def human(self) -> list[FieldResult]:
        return [f for f in self.fields if f.human]


def manual(reason: str) -> Classification:
    return Classification(cls="manual", reasons=[reason])


def _type_key(f: FormField) -> str | None:
    if f.type == "email":
        return "identity.email"
    if f.type == "phone":
        return "identity.phone"
    if f.type == "file":
        low = f.label.lower()
        if "cover" in low:
            return "files.cover_letter"
        if "resume" in low or re.search(r"\bcv\b", low):
            return "files.resume"
    return None


def _human_reasons(f: FormField, ats: str, eeo_confirmed: bool) -> list[str]:
    out = []
    if _ATTEST.search(f.label) or (f.description and _ATTEST.search(f.description)):
        out.append("attestation or policy question: answer it yourself")
    if f.section == "consent":
        out.append("consent: your call each time")
    if f.section in ("eeo", "survey") and not eeo_confirmed:
        out.append("voluntary self-identification: review it yourself (or set it in the form)")
    if ats == "ashby" and f.required and f.type in ("boolean", "select", "radio"):
        out.append("possible knockout: Ashby can auto-reject on this answer")
    if ats == "lever" and f.type in ("radio", "checkbox"):
        out.append("Lever: you tick this yourself; the recommended value is shown")
    return out


def _options_for(key: str, value: str | list[str], matcher: Matcher) -> list[str]:
    vals = value if isinstance(value, list) else [value]
    spec = BUILTIN.get(key)
    out: list[str] = []
    entry = next((e for e in matcher.answers.bank if e.key == key), None)
    if entry:
        out += entry.options
    for v in vals:
        out.append(v)
        if spec:
            out += spec.option_aliases(v)
            if key == "identity.country":
                from jobhunt.bank.keys import country_names

                out += country_names(v)
    return out


def _fit_options(f: FormField, key: str, value: str | list[str], matcher: Matcher) -> bool:
    if f.type == "boolean":
        return value in ("yes", "no")
    if not f.options:
        return True
    spec = BUILTIN.get(key)
    yesno = bool(spec and spec.kind == "yesno")
    if spec and spec.pick and isinstance(value, str):
        return spec.pick(value, f.options) is not None
    if isinstance(value, list):
        hits = [choose_option(_options_for(key, v, matcher), f.options) for v in value]
        return any(hits)
    if choose_option(_options_for(key, value, matcher), f.options, yesno=yesno):
        return True
    if value == DECLINE_EEO or value in DECLINE_ALIASES:
        declines = [o for o in f.options if _DECLINE_RE.search(normalize(o))]
        return len(declines) == 1
    return False


def resolve_field(
    f: FormField, ctx: AnswerContext, matcher: Matcher, *, ats: str, eeo_confirmed: bool
) -> FieldResult:
    m: Match = matcher.match(f.label, field_name=f.id, company=ctx.company)
    if not m.confident and (tk := _type_key(f)):
        m = Match(tk, "exact", 1.0, "field_type")
    human = _human_reasons(f, ats, eeo_confirmed)
    spec = BUILTIN.get(m.key or "")
    entry = next((e for e in matcher.answers.bank if e.key == m.key), None)
    sensitive = bool(
        (spec and spec.sensitive) or (entry and entry.sensitive)
    ) or is_sensitive_label(f.label)
    base = {
        "id": f.id,
        "label": f.label,
        "type": f.type,
        "required": f.required,
        "section": f.section,
        "key": m.key,
        "match": m.status,
        "via": m.via,
        "score": m.score,
        "human": human,
        "sensitive": sensitive,
        "max_length": f.max_length,
        "options": f.options[:50],
        "description": f.description,
    }

    def out(res: Resolution, reason: str | None = None) -> FieldResult:
        if not f.required and res in BLOCKING:
            res, reason = "skipped", reason or "optional: left blank"
        return FieldResult(**base, resolution=res, reason=reason)

    is_human_only = any(h.startswith(("attestation", "consent", "voluntary")) for h in human)
    if m.status == "candidate":
        if is_human_only:
            return out("human_only")
        if spec and spec.kind == "essay":  # looks like an essay prompt: it is one either way
            return out("essay" if f.required else "skipped", "free text you write")
        return out("candidate", f"looks like “{m.wording}” ({m.key}); confirm it once")
    if m.status == "unknown" or m.key is None:
        if is_human_only:
            return out("human_only")
        if f.type == "textarea" or (f.type in ("text", "file") and _ESSAY_LABEL.search(f.label)):
            return out("essay", "free text you write")
        return out("unknown", "no saved answer for this question")
    if spec and spec.eeo and not eeo_confirmed:
        return out("human_only")
    if (spec and spec.kind == "essay") or (entry is None and spec is None and f.type == "textarea"):
        if not f.required:
            return out("skipped", "optional free text")
        return out("essay", "free text you write")
    value = spec.resolve(ctx) if spec else (entry.answer if entry else None)
    if value in (None, "", []):
        if is_human_only:
            return out("human_only")
        q = spec.question if spec else (entry.question if entry else m.key)
        return out("unanswered", f"not in your answers yet: {q}")
    assert value is not None
    if spec and spec.kind == "file":
        return out("answered")
    if f.type in ("select", "multiselect", "radio", "checkbox", "boolean") and not _fit_options(
        f, m.key, value, matcher
    ):
        return out("option_mismatch", "your answer doesn't match any option on this form")
    if f.max_length and isinstance(value, str) and len(value) > f.max_length:
        return out("too_long", f"{len(value)} characters; the field allows {f.max_length}")
    return out("answered")


def classify(
    spec: FormSpec, ctx: AnswerContext, matcher: Matcher, *, eeo_confirmed: bool
) -> Classification:
    if spec.blocked:
        return manual(spec.blocked)
    fields = [
        resolve_field(f, ctx, matcher, ats=spec.ats, eeo_confirmed=eeo_confirmed)
        for f in spec.fields
    ]
    c = Classification(cls="ready", fields=fields, captcha_at_submit=spec.captcha_at_submit)
    essays = [f for f in fields if f.required and f.resolution == "essay"]
    needs = c.needs
    if essays:
        c.cls = "essay"
        c.reasons.append(f"{len(essays)} required free-text answer(s)")
    if needs:
        if c.cls == "ready":
            c.cls = "needs_answers"
        c.reasons.append(f"{len(needs)} required field(s) need an answer from you")
    if c.cls == "ready":
        c.reasons.append("every required field has a confirmed answer")
    return c


def fill_value(
    fr: FieldResult, ctx: AnswerContext, matcher: Matcher, options: list[str] | None = None
) -> str | list[str] | None:
    """The value for an answered field, as the form takes it: the form's own option text(s) for
    choice fields (a list for multi-selects and checkboxes), the full path for files, else text.
    ``options`` is the form's full list (``fr.options`` keeps only the first 50).
    Computed locally for the packet and the pre-fill engine; never put in an envelope."""
    if fr.resolution != "answered" or not fr.key:
        return None
    opts = fr.options if options is None else options
    spec = BUILTIN.get(fr.key)
    entry = next((e for e in matcher.answers.bank if e.key == fr.key), None)
    value = spec.resolve(ctx) if spec else (entry.answer if entry else None)
    if value in (None, "", []):
        return None
    if spec and spec.kind == "file":
        return str(value)
    if fr.type == "boolean":
        return {"yes": "Yes", "no": "No"}.get(str(value), str(value))
    if opts and fr.type in ("select", "multiselect", "radio", "checkbox"):
        vals = value if isinstance(value, list) else [value]
        yesno = bool(spec and spec.kind == "yesno")
        if spec and spec.pick and isinstance(value, str):
            return spec.pick(value, opts)
        picked = [choose_option(_options_for(fr.key, v, matcher), opts, yesno=yesno) for v in vals]
        chosen = [p for p in picked if p]
        if not chosen and (value == DECLINE_EEO or value in DECLINE_ALIASES):
            chosen = [o for o in opts if _DECLINE_RE.search(normalize(o))][:1]
        if not chosen:
            return None
        return chosen if fr.type in ("multiselect", "checkbox") else chosen[0]
    return ", ".join(value) if isinstance(value, list) else str(value)


def display_value(fr: FieldResult, ctx: AnswerContext, matcher: Matcher) -> str | None:
    """The text to paste or pick for an answered field (file fields show the file name)."""
    value = fill_value(fr, ctx, matcher)
    if value is None:
        return None
    if isinstance(value, list):
        return "; ".join(value)
    spec = BUILTIN.get(fr.key or "")
    if spec and spec.kind == "file":
        return Path(value).name
    return value
