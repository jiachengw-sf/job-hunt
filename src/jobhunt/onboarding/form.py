"""The onboarding form: preferences.yaml and answers.yaml in one local page.

Fields are declared once (``PREF_FIELDS`` / ``ANSWER_FIELDS``) and drive pre-fill, rendering and
parsing. Every field can be left empty. Each preference has a Must box (a hard gate); unticked is
Nice. Sensitive answers (work authorization, EEO, ...) are entered here and nowhere else.

Values are validated against the same pydantic models the rest of jobhunt loads, so the form
can't write a file the CLI would reject.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from jobhunt.bank.keys import BUILTIN
from jobhunt.bank.match import normalize
from jobhunt.models import Answers, Preferences, Profile
from jobhunt.models.answers import DECLINE_EEO, BankEntry
from jobhunt.paths import secure_file

Widget = Literal[
    "text", "list", "lines", "date", "int", "float", "yesno", "yesnodecline", "bool", "select",
    "multi", "textarea", "check",
]  # fmt: skip


@dataclass(frozen=True)
class F:
    path: str  # dotted path inside the model
    label: str
    widget: Widget = "text"
    options: tuple[str, ...] = ()
    help: str = ""
    pref: bool = False  # a preference with a Must box
    section: str = ""


SENIORITY = ("internship", "new_grad", "entry", "mid", "senior")
EMPLOYMENT = ("internship", "co-op", "full-time", "part-time", "contract")
MODES = ("onsite", "hybrid", "remote")

PREF_FIELDS: list[F] = [
    F("roles.families", "Role families", "list", help="e.g. software engineering, data analysis", pref=True, section="What"),
    F("roles.include_keywords", "Title must mention one of", "list", help="e.g. intern, backend", pref=True, section="What"),
    F("roles.exclude_keywords", "Skip titles mentioning", "list", help="e.g. senior, manager", pref=True, section="What"),
    F("roles.seniority", "Seniority", "multi", SENIORITY, pref=True, section="What"),
    F("employment_types", "Employment type", "multi", EMPLOYMENT, pref=True, section="Employment type"),
    F("timing.terms", "Target terms", "list", help="e.g. Summer 2027", pref=True, section="Term and timing"),
    F("timing.earliest_start", "Earliest start", "date", pref=True, section="Term and timing"),
    F("timing.latest_end", "Latest end", "date", pref=True, section="Term and timing"),
    F("timing.min_duration_weeks", "Minimum duration (weeks)", "int", pref=True, section="Term and timing"),
    F("timing.max_duration_weeks", "Maximum duration (weeks)", "int", pref=True, section="Term and timing"),
    F("timing.hours_per_week", "Hours per week", "int", pref=True, section="Term and timing"),
    F("location.places", "Places", "lines", help="One per line, optional radius in miles: Chicago, IL | 25", pref=True, section="Where"),
    F("location.work_modes", "Work modes", "multi", MODES, pref=True, section="Where"),
    F("location.relocation", "Open to relocating", "bool", pref=True, section="Where"),
    F("location.countries", "Countries", "list", help="ISO codes, e.g. US, CA. Sets which work-authorization questions you see below.", pref=True, section="Where"),
    F("pay.min", "Minimum pay", "float", pref=True, section="Pay"),
    F("pay.ideal", "Ideal pay", "float", section="Pay"),
    F("pay.unit", "Per", "select", ("annual", "hourly"), section="Pay"),
    F("pay.currency", "Currency", "text", help="e.g. USD", section="Pay"),
    F("company.sizes", "Company sizes", "list", help="e.g. startup, mid-size, large", pref=True, section="Company"),
    F("company.stages", "Company stages", "list", pref=True, section="Company"),
    F("company.prefer_industries", "Industries you prefer", "list", pref=True, section="Company"),
    F("company.avoid_industries", "Industries to avoid", "list", pref=True, section="Company"),
    F("company.blocklist", "Never show these companies", "list", section="Company"),
    F("dealbreakers", "Dealbreakers", "lines", help="One per line. Always a hard rule.", section="Dealbreakers"),
]  # fmt: skip

EEO_CHOICES: dict[str, tuple[str, ...]] = {
    "gender": (DECLINE_EEO, "Male", "Female", "Non-binary"),
    "race_ethnicity": (DECLINE_EEO, "Hispanic or Latino", "White", "Black or African American", "Asian", "Native Hawaiian or Other Pacific Islander", "American Indian or Alaska Native", "Two or More Races"),
    "hispanic_latino": (DECLINE_EEO, "Yes", "No"),
    "veteran_status": (DECLINE_EEO, "I am not a protected veteran", "I identify as one or more of the classifications of protected veteran"),
    "disability_status": (DECLINE_EEO, "No, I don't have a disability", "Yes, I have a disability (or previously had a disability)"),
    "sexual_orientation": (DECLINE_EEO, "Heterosexual", "Gay or Lesbian", "Bisexual", "Asexual", "Queer"),
    "transgender": (DECLINE_EEO, "No", "Yes"),
}  # fmt: skip

ANSWER_FIELDS: list[F] = [
    F("identity.legal_first_name", "Legal first name", section="Identity and contact"),
    F("identity.legal_last_name", "Legal last name", section="Identity and contact"),
    F("identity.preferred_name", "Preferred name", section="Identity and contact"),
    F("identity.pronouns", "Pronouns (optional)", section="Identity and contact"),
    F("identity.email", "Email", section="Identity and contact"),
    F("identity.phone", "Phone", section="Identity and contact"),
    F("identity.phone_country", "Phone country (ISO, e.g. US)", section="Identity and contact"),
    F("identity.address_line1", "Street address", section="Identity and contact"),
    F("identity.address_line2", "Address line 2", section="Identity and contact"),
    F("identity.city", "City", section="Identity and contact"),
    F("identity.region", "State / province", section="Identity and contact"),
    F("identity.postal_code", "Postal code", section="Identity and contact"),
    F("identity.country", "Country (ISO, e.g. US)", section="Identity and contact"),
    F("identity.over_18", "18 or older", "yesno", section="Identity and contact"),
    F("links.linkedin", "LinkedIn URL", section="Links"),
    F("links.github", "GitHub URL", section="Links"),
    F("links.portfolio", "Website / portfolio", section="Links"),
    F("education.class_standing", "Class standing", "select", ("Freshman", "Sophomore", "Junior", "Senior", "Graduate", "Graduated"), section="Education"),
    F("education.graduation_month", "Graduation month (1-12)", "int", section="Education"),
    F("education.graduation_year", "Graduation year", "int", section="Education"),
    F("education.gpa", "GPA", "float", section="Education"),
    F("education.gpa_scale", "GPA scale", "float", help="e.g. 4.0", section="Education"),
    F("education.gpa_disclose_at_or_above", "Only share GPA when at least", "float", section="Education"),
    F("availability.start_date", "Earliest start date", "date", section="Availability"),
    F("availability.end_date", "End date (internships)", "date", section="Availability"),
    F("availability.terms", "Terms available", "list", section="Availability"),
    F("availability.full_term", "Available for the full term", "yesno", section="Availability"),
    F("availability.hours_per_week", "Hours per week", "int", section="Availability"),
    F("availability.notice_period", "Notice period", help="e.g. 2 weeks", section="Availability"),
    F("location.willing_to_relocate", "Willing to relocate", "yesno", section="Location and travel"),
    F("location.willing_to_travel", "Willing to travel", help="e.g. Up to 25%", section="Location and travel"),
    F("compensation.expected", "Expected pay", help="As you'd write it on a form, e.g. $40/hour", section="Compensation"),
    F("compensation.prefer_not_to_say", "Prefer not to say", "check", section="Compensation"),
    F("experience.years", "Years of professional experience", "float", section="Experience"),
    F("experience.prior_employers", "Employers you've worked for", "list", help="Used to answer “have you worked here before?”", section="Experience"),
    F("skills.certifications", "Certifications", "list", section="Skills and languages"),
    F("skills.spoken_languages", "Languages you speak", "list", section="Skills and languages"),
    F("standard.relatives_at_company", "Relatives at companies you apply to", "yesno", help="Answered per application; set your usual answer", section="Standard questions"),
    F("standard.non_compete", "Bound by a non-compete", "yesno", section="Standard questions"),
    F("standard.security_clearance", "Security clearance", help="e.g. none, Secret", section="Standard questions"),
    F("standard.background_check_consent", "Background check consent", "yesnodecline", section="Standard questions"),
    F("standard.drug_test_consent", "Drug test consent", "yesnodecline", section="Standard questions"),
    F("standard.sms_consent", "Text message (SMS) consent", "yesno", section="Standard questions"),
    F("sourcing.how_did_you_hear", "Default “how did you hear about us”", help="e.g. Job board, Company website", section="Sourcing"),
    F("sourcing.referral_default", "Default referral answer", help="e.g. No", section="Sourcing"),
    *[F(f"eeo.{k}", k.replace("_", " ").capitalize(), "select", v, section="Voluntary self-identification (US)") for k, v in EEO_CHOICES.items()],
    F("eeo.confirmed", "Fill these on applications for me (otherwise each form leaves them to you)", "check", section="Voluntary self-identification (US)"),
    F("base_answers.bio", "Short bio", "textarea", section="Base answers"),
    F("base_answers.why_field", "Why this field", "textarea", section="Base answers"),
    F("base_answers.project", "A project you'd tell an interviewer about", "textarea", section="Base answers"),
]  # fmt: skip

WA_KEYS = (
    ("authorized", "Authorized to work here", "yesno"),
    ("citizen", "Citizen of this country", "yesno"),
    ("sponsorship_now", "Need visa sponsorship now", "yesno"),
    ("sponsorship_future", "Will need sponsorship in the future", "yesno"),
    ("student_status", "Student work-authorization status (optional)", "text"),
)
WA_COUNTRIES = ("US", "CA", "GB", "IE", "DE", "FR", "NL", "IN", "SG", "AU", "JP", "CH")


# --- reading values in and out ---------------------------------------------------------------


def _get(d: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(part)
    return d


def _set(d: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def _del(d: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        d = d.get(p)  # type: ignore[assignment]
        if not isinstance(d, dict):
            return
    d.pop(parts[-1], None)


def _to_text(f: F, v: Any) -> str | list[str]:
    if v is None:
        return ""
    if f.widget == "multi":
        return [str(x) for x in v]
    if f.widget == "list":
        return ", ".join(str(x) for x in v)
    if f.widget == "lines":
        if f.path == "location.places":
            return "\n".join(
                p["name"] + (f" | {p['radius_miles']:g}" if p.get("radius_miles") else "")
                for p in v
            )
        return "\n".join(str(x) for x in v)
    if f.widget == "bool":
        return "yes" if v else "no"
    if f.widget == "check":
        return "on" if v else ""
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def prefill(
    prefs: dict[str, Any] | None, answers: dict[str, Any] | None, *, profile: Profile | None,
    contact: dict[str, Any] | None,
) -> dict[str, Any]:  # fmt: skip
    """Form values keyed by input name. Existing files win; otherwise the resume fills what it
    can (contact details, education, languages, experience)."""
    vals: dict[str, Any] = {}
    prefs = prefs or {}
    for f in PREF_FIELDS:
        raw = _get(prefs, f.path)
        if f.path.startswith("pay."):
            pay = prefs.get("pay")
            inner = pay.get("value", pay) if isinstance(pay, dict) else None
            raw = (inner or {}).get(f.path.split(".")[1]) if isinstance(inner, dict) else None
            if f.pref and isinstance(pay, dict) and pay.get("importance") == "must":
                vals[f"p:{f.path}.__must"] = "on"
        elif isinstance(raw, dict) and "value" in raw:
            if raw.get("importance") == "must":
                vals[f"p:{f.path}.__must"] = "on"
            raw = raw["value"]
        vals[f"p:{f.path}"] = _to_text(f, raw)
    a = answers or {}
    if not answers:
        c = contact or {}
        name = (c.get("name") or "").split()
        a = {
            "identity": {
                "legal_first_name": name[0] if name else None,
                "legal_last_name": " ".join(name[1:]) or None,
                "email": c.get("email"),
                "phone": c.get("phone"),
            },
            "links": c.get("links") or {},
        }
        if profile and profile.education:
            grad = profile.education[0].graduation or ""
            if m := re.match(r"(\d{4})-(\d{2})", grad):
                a["education"] = {
                    "graduation_year": int(m.group(1)),
                    "graduation_month": int(m.group(2)),
                }
        if profile:
            a["experience"] = {"prior_employers": sorted({e.org for e in profile.experience})}
    for f in ANSWER_FIELDS:
        v = _get(a, f.path)
        if v is None and f.path.startswith("eeo.") and f.widget == "select":
            v = DECLINE_EEO
        vals[f"a:{f.path}"] = _to_text(f, v)
    for cc, wa in (a.get("work_authorization") or {}).items():
        for k, _, _ in WA_KEYS:
            vals[f"a:work_authorization.{cc}.{k}"] = wa.get(k) or ""
    return vals


def _parse_value(f: F, raw: list[str]) -> tuple[Any, str | None]:
    """(value or None when empty, error)."""
    if f.widget == "multi":
        return ([x for x in raw if x in f.options] or None), None
    s = (raw[0] if raw else "").strip()
    if f.widget == "check":
        return s == "on", None
    if not s:
        return None, None
    try:
        if f.widget == "list":
            return [x.strip() for x in s.split(",") if x.strip()] or None, None
        if f.widget == "lines":
            lines = [x.strip() for x in s.splitlines() if x.strip()]
            if f.path == "location.places":
                out = []
                for ln in lines:
                    name, _, radius = ln.partition("|")
                    place: dict[str, Any] = {"name": name.strip()}
                    if radius.strip():
                        place["radius_miles"] = float(radius)
                    out.append(place)
                return out, None
            return lines, None
        if f.widget == "date":
            return date.fromisoformat(s).isoformat(), None
        if f.widget == "int":
            return int(s), None
        if f.widget == "float":
            return float(s), None
        if f.widget == "bool":
            return s == "yes", None
        if f.widget in ("yesno", "yesnodecline", "select") and f.options and s not in f.options:
            return None, f"{f.label}: pick one of the options"
        return s, None
    except ValueError:
        return None, f"{f.label}: “{s}” isn't valid"


def parse(
    form: dict[str, list[str]],
    *,
    existing_prefs: dict[str, Any] | None = None,
    existing_answers: dict[str, Any] | None = None,
    new_questions: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Form data merged over the existing files -> (preferences, answers, errors). Keys the form
    doesn't manage are kept as they were. Nothing is written here."""
    import copy

    errors: list[str] = []
    prefs: dict[str, Any] = copy.deepcopy(existing_prefs or {})
    prefs.setdefault("version", 1)
    prefs.pop("pay", None)
    pay: dict[str, Any] = {}
    for f in PREF_FIELDS:
        v, err = _parse_value(f, form.get(f"p:{f.path}", []))
        if err:
            errors.append(err)
        if not f.path.startswith("pay."):
            _del(prefs, f.path)
        if f.path.startswith("pay."):
            if v is not None:
                pay[f.path.split(".")[1]] = v
            continue
        if v is None or v == []:
            continue
        if f.pref and form.get(f"p:{f.path}.__must", [""])[0] == "on":
            v = {"value": v, "importance": "must"}
        _set(prefs, f.path, v)
    if pay.get("min") is not None or pay.get("ideal") is not None:
        must = form.get("p:pay.min.__must", [""])[0] == "on"
        prefs["pay"] = {"value": pay, "importance": "must"} if must else pay

    answers: dict[str, Any] = copy.deepcopy(existing_answers or {})
    answers.setdefault("version", 1)
    answers.pop("work_authorization", None)
    existing_bank = [BankEntry.model_validate(e) for e in answers.pop("bank", None) or []]
    for f in ANSWER_FIELDS:
        v, err = _parse_value(f, form.get(f"a:{f.path}", []))
        if err:
            errors.append(err)
        _del(answers, f.path)
        if v is None or (f.widget == "check" and v is False):
            continue
        _set(answers, f.path, v)
    wa: dict[str, dict[str, Any]] = {}
    for name, raw in form.items():
        m = re.fullmatch(r"a:work_authorization\.([A-Z]{2})\.(\w+)", name)
        if not m or not raw or not raw[0].strip():
            continue
        cc, k = m.groups()
        if k not in {x[0] for x in WA_KEYS}:
            continue
        val = raw[0].strip()
        if k != "student_status" and val not in ("yes", "no"):
            errors.append(f"Work authorization ({cc}): pick yes or no")
            continue
        wa.setdefault(cc, {})[k] = val
    if wa:
        answers["work_authorization"] = wa

    bank = [e.model_dump(mode="json", exclude_defaults=True) for e in existing_bank]
    by_key = {e["key"]: e for e in bank}
    for i, q in enumerate(new_questions):
        label = q["label"]
        same = form.get(f"nq:{i}:same", [""])[0]
        ans = form.get(f"nq:{i}:answer", [""])[0].strip()
        if same == "yes" and q.get("candidate_key"):
            key = q["candidate_key"]
            e = by_key.get(key)
            if e is None:
                spec = BUILTIN.get(key)
                e = {"key": key, "question": spec.question if spec else label, "wordings": []}
                bank.append(e)
                by_key[key] = e
            if label not in e.setdefault("wordings", []):
                e["wordings"].append(label)
            continue
        if not ans:
            continue
        key = "custom." + (re.sub(r"[^a-z0-9]+", "_", normalize(label))[:48].strip("_") or f"q{i}")
        e = by_key.get(key) or {"key": key, "question": label, "wordings": []}
        e["answer"] = ans
        if label not in e["wordings"]:
            e["wordings"].append(label)
        if q.get("options") and ans in q["options"]:
            e["options"] = [ans]
        if q.get("sensitive"):
            e["sensitive"] = True
        if key not in by_key:
            bank.append(e)
            by_key[key] = e
    if bank:
        answers["bank"] = bank
    return prefs, answers, errors


def validate(prefs: dict[str, Any], answers: dict[str, Any]) -> list[str]:
    errors = []
    for model, data, name in ((Preferences, prefs, "Preferences"), (Answers, answers, "Answers")):
        try:
            model.model_validate(data)
        except ValidationError as e:
            for err in e.errors():
                loc = ".".join(str(x) for x in err["loc"])
                errors.append(f"{name} {loc}: {err['msg']}")
    return errors


def write_yaml(path: Path, data: dict[str, Any], header: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    tmp.write_text(f"# {header}\n{body}", encoding="utf-8")
    tmp.replace(path)
    secure_file(path)


# --- rendering ---------------------------------------------------------------------------------


def _e(s: object) -> str:
    return html.escape(str(s), quote=True)


def _fid(name: str) -> str:
    return "f-" + re.sub(r"[^A-Za-z0-9_-]+", "-", name)


def _input(name: str, f: F, val: Any) -> str:
    v = val if val is not None else ""
    idattr = f'id="{_fid(name)}"'
    if f.widget == "multi":
        chosen = set(v or [])
        boxes = "".join(
            f'<label class="chip"><input type="checkbox" name="{_e(name)}" value="{_e(o)}"'
            f"{' checked' if o in chosen else ''}> {_e(o.replace('_', ' '))}</label>"
            for o in f.options
        )
        return f'<div class="chips" role="group" aria-label="{_e(f.label)}">{boxes}</div>'
    if f.widget in ("textarea", "lines"):
        return f'<textarea {idattr} name="{_e(name)}" rows="{4 if f.widget == "textarea" else 3}">{_e(v)}</textarea>'
    if f.widget == "check":
        return (
            f'<input type="checkbox" {idattr} name="{_e(name)}"{" checked" if v == "on" else ""}>'
        )
    opts: Iterable[str] | None = None
    if f.widget == "yesno":
        opts = ("yes", "no")
    elif f.widget == "yesnodecline":
        opts = ("yes", "no", "decline")
    elif f.widget == "bool":
        opts = ("yes", "no")
    elif f.widget == "select":
        opts = f.options
    if opts is not None:
        options = '<option value="">—</option>' + "".join(
            f'<option value="{_e(o)}"{" selected" if o == v else ""}>{_e(o)}</option>' for o in opts
        )
        return f'<select {idattr} name="{_e(name)}">{options}</select>'
    t = {"date": "date", "int": "number", "float": "number"}.get(f.widget, "text")
    step = ' step="any"' if f.widget == "float" else ""
    return f'<input type="{t}" {idattr} name="{_e(name)}" value="{_e(v)}"{step}>'


def _row(prefix: str, f: F, vals: dict[str, Any]) -> str:
    name = f"{prefix}:{f.path}"
    must = ""
    if f.pref:
        checked = " checked" if vals.get(f"{name}.__must") == "on" else ""
        must = (
            f'<label class="must" title="Must = a hard filter. Unticked = Nice (affects '
            f'ranking only)."><input type="checkbox" name="{_e(name)}.__must"{checked} '
            f'aria-label="{_e(f.label)}: Must (a hard filter)"> Must'
            f"</label>"
        )
    hint = f'<div class="hint">{_e(f.help)}</div>' if f.help else ""
    # a chip group isn't a single control: its name comes from its aria-label instead
    if f.widget == "multi":
        head = f'<div class="field"><div class="lbl">{_e(f.label)}</div>'
    else:
        head = f'<div class="field"><label class="lbl" for="{_fid(name)}">{_e(f.label)}</label>'
    return f'{head}<div class="ctl">{_input(name, f, vals.get(name))}{must}{hint}</div></div>'


def _sections(prefix: str, fields: list[F], vals: dict[str, Any]) -> list[tuple[str, str]]:
    order: dict[str, list[str]] = {}
    for f in fields:
        order.setdefault(f.section, []).append(_row(prefix, f, vals))
    return [(sec, "".join(rows)) for sec, rows in order.items()]


def _wa_section(vals: dict[str, Any], countries: list[str]) -> str:
    present = {k.split(".")[1] for k in vals if k.startswith("a:work_authorization.")}
    ccs = list(dict.fromkeys([*countries, *sorted(present), *WA_COUNTRIES]))
    blocks = []
    for cc in ccs:
        rows = "".join(
            _row("a", F(f"work_authorization.{cc}.{k}", label, widget), vals)  # type: ignore[arg-type]
            for k, label, widget in WA_KEYS
        )
        hidden = "" if cc in countries or cc in present else " hidden"
        blocks.append(
            f'<fieldset class="wa" data-cc="{cc}"{hidden}><legend>{cc}</legend>{rows}</fieldset>'
        )
    return "".join(blocks)


def _new_questions(qs: list[dict[str, Any]]) -> str:
    if not qs:
        return '<p class="hint">No unanswered application questions yet. They appear here after `jobhunt inspect`.</p>'
    rows = []
    for i, q in enumerate(qs):
        seen = f'<span class="hint">on {q.get("jobs", 1)} job(s)</span>'
        same = ""
        if q.get("candidate_key"):
            spec = BUILTIN.get(q["candidate_key"])
            canon = spec.question if spec else q["candidate_key"]
            same = (
                f'<div class="same">Same question as “{_e(canon)}”? '
                f'<label><input type="radio" name="nq:{i}:same" value="yes"> Yes, reuse that answer</label> '
                f'<label><input type="radio" name="nq:{i}:same" value="no"> No</label></div>'
            )
        opts = q.get("options") or []
        if opts:
            ctl = (
                f'<select id="nq-{i}" name="nq:{i}:answer"><option value="">—</option>'
                + "".join(f'<option value="{_e(o)}">{_e(o)}</option>' for o in opts)
                + "</select>"
            )
        else:
            ctl = f'<input type="text" id="nq-{i}" name="nq:{i}:answer">'
        rows.append(
            f'<div class="field"><label class="lbl" for="nq-{i}">{_e(q["label"])} {seen}</label>'
            f'<div class="ctl">{same}{ctl}</div></div>'
        )
    return "".join(rows)


CSS = """
:root{--bg:#f7f6f2;--fg:#1d1d1b;--muted:#6b6a65;--line:#e2dfd6;--card:#fff;--accent:#1f5f8b;--warn:#a1460b;--ok:#2f6b3a}
@media (prefers-color-scheme:dark){:root{--bg:#151513;--fg:#ecebe6;--muted:#a3a29b;--line:#33322e;--card:#1e1e1b;--accent:#7fb6dd;--warn:#f0a35e;--ok:#8fcf98}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px}
header .in{max-width:880px;margin:0 auto;display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}
h1{font-size:18px;margin:0}main{max-width:880px;margin:0 auto;padding:16px}
nav{font-size:13px;color:var(--muted);display:flex;flex-wrap:wrap;gap:4px 12px;margin:4px 0 0}nav a{color:var(--muted)}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:14px 0}
h2{font-size:16px;margin:0 0 4px}h3{font-size:14px;margin:14px 0 6px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.field{display:grid;grid-template-columns:minmax(150px,240px) 1fr;gap:6px 14px;padding:7px 0;border-top:1px solid var(--line)}
.field:first-of-type{border-top:0}.lbl{font-weight:500;padding-top:5px}.ctl{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
input[type=text],input[type=number],input[type=date],select,textarea{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:5px 8px;min-width:0;flex:1 1 260px}
textarea{width:100%;flex-basis:100%}.chips{display:flex;flex-wrap:wrap;gap:6px}.chip{border:1px solid var(--line);border-radius:14px;padding:2px 10px;font-size:13px}
.must{font-size:12px;color:var(--warn);white-space:nowrap}.hint{flex-basis:100%;font-size:12px;color:var(--muted)}
.note{font-size:13px;color:var(--muted)}.sens{border-left:3px solid var(--warn)}fieldset.wa{border:1px dashed var(--line);border-radius:8px;margin:8px 0;padding:4px 10px}
legend{font-weight:600;font-size:13px}button{font:inherit;font-weight:600;padding:7px 18px;border-radius:8px;border:0;background:var(--accent);color:#fff;cursor:pointer}
.err{background:#fde8e1;color:#7a2a07;border-radius:8px;padding:10px 14px}.okmsg{background:#e5f3e7;color:#1f4d27;border-radius:8px;padding:10px 14px}
@media (prefers-color-scheme:dark){.err{background:#3b1d12;color:#f7c5ab}.okmsg{background:#173020;color:#bfe6c5}}
.same{flex-basis:100%;font-size:13px}.saved{color:var(--ok);font-weight:600;margin-right:10px}@media (max-width:640px){.field{grid-template-columns:1fr}}
"""

JS = """
function syncCountries(){const v=(document.querySelector('[name="p:location.countries"]').value||'').toUpperCase().split(/[ ,]+/).filter(Boolean);
document.querySelectorAll('fieldset.wa').forEach(fs=>{const keep=v.includes(fs.dataset.cc)||[...fs.querySelectorAll('select,input')].some(x=>x.value);fs.hidden=!keep});
const us=document.getElementById('sec-eeo');if(us){us.hidden=!(v.length===0||v.includes('US'))}}
document.addEventListener('input',e=>{if(e.target.name==='p:location.countries')syncCountries()});syncCountries();
"""


def render(
    vals: dict[str, Any],
    *,
    token: str,
    new_questions: list[dict[str, Any]],
    errors: list[str] | None = None,
    saved: list[str] | None = None,
) -> str:
    countries = [
        c.strip().upper()
        for c in str(vals.get("p:location.countries") or "").replace(" ", ",").split(",")
        if c.strip()
    ]
    pref_secs = _sections("p", PREF_FIELDS, vals)
    ans_secs = _sections("a", ANSWER_FIELDS, vals)
    sensitive = {
        "Identity and contact",
        "Compensation",
        "Standard questions",
        "Voluntary self-identification (US)",
    }
    parts = []
    nav = []
    for sec, rows in pref_secs:
        sid = "sec-" + re.sub(r"[^a-z]+", "-", sec.lower())
        nav.append(f'<a href="#{sid}">{_e(sec)}</a>')
        parts.append(f'<section id="{sid}"><h2>{_e(sec)}</h2>{rows}</section>')
    parts.append(
        '<h3>Screening questions</h3><p class="note">Every field is optional. These answers are filled into forms by code; the sensitive ones (marked) are never shown to your AI tool.</p>'
    )
    for sec, rows in ans_secs:
        sid = (
            "sec-eeo"
            if sec.startswith("Voluntary")
            else "sec-" + re.sub(r"[^a-z]+", "-", sec.lower())
        )
        nav.append(f'<a href="#{sid}">{_e(sec)}</a>')
        cls = ' class="sens"' if sec in sensitive else ""
        extra = ""
        if sec.startswith("Voluntary"):
            extra = '<p class="note">Voluntary. Each defaults to “Decline to self-identify”. Answering never affects which jobs jobhunt shows you.</p>'
        parts.append(f'<section id="{sid}"{cls}><h2>{_e(sec)}</h2>{extra}{rows}</section>')
        if sec == "Education":
            parts.append(
                '<section id="sec-work-auth" class="sens"><h2>Work authorization</h2>'
                '<p class="note">One block per country in “Countries” above, in the yes/no wording forms use.</p>'
                f"{_wa_section(vals, countries)}</section>"
            )
            nav.append('<a href="#sec-work-auth">Work authorization</a>')
    parts.append(
        f'<section id="sec-new"><h2>Questions from your matches</h2><p class="note">Found on application forms with no saved answer. Answer the ones you can; skip the rest.</p>{_new_questions(new_questions)}</section>'
    )
    nav.append('<a href="#sec-new">New questions</a>')
    msg = ""
    if errors:
        msg = (
            '<div class="err"><b>Not saved yet.</b><ul>'
            + "".join(f"<li>{_e(e)}</li>" for e in errors)
            + "</ul></div>"
        )
    elif saved:
        msg = (
            '<div class="okmsg"><b>Saved.</b> '
            + _e(", ".join(saved))
            + ". You can close this tab, or keep editing and save again.</div>"
        )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>jobhunt setup</title>
<style>{CSS}</style></head><body>
<form method="post" action="/save" autocomplete="off">
<header><div class="in"><div><h1>jobhunt setup</h1><nav>{"".join(nav)}</nav></div>
<div>{'<span class="saved">Saved ✓</span> ' if saved and not errors else ""}<button type="submit">Save</button></div></div></header>
<main>{msg}<input type="hidden" name="token" value="{_e(token)}">
<p class="note">Your preferences and screening answers. Saved to your computer only (~/.jobhunt). Skip anything you like.</p>
{"".join(parts)}
<p><button type="submit">Save</button></p></main></form>
<script>{JS}</script></body></html>"""


def posted_values(form: dict[str, list[str]]) -> dict[str, Any]:
    """Posted form data as render values (to show the form again with its errors)."""
    multis = {f"p:{f.path}" for f in PREF_FIELDS if f.widget == "multi"}
    return {k: (v if k in multis else v[0]) for k, v in form.items() if k != "token"}


def load_new_questions(path: Path, limit: int = 25) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("questions") or [])[:limit]
