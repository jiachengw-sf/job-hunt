"""Regenerate the offline application-form fixtures:

    uv run python tests/fixtures/forms/make_forms.py

Every company, form and question here is synthetic; the shapes follow what each ATS serves
(Greenhouse ``jobs/{id}?questions=true`` JSON, Lever's server-rendered ``/apply`` page with card
templates, the ``jobPosting`` object an Ashby application page loads). ``overlay/`` is laid over
``tests/fixtures/sources/base`` (it replaces the Greenhouse job files with question-bearing ones).

``pages/`` holds what the pre-fill browser loads: each job's application page, built from the
same questions, with small scripts (``js/``) that copy each ATS's widgets and quirks, plus the
endpoint responses the pages call (``<path>.POST.json``; ``_default.POST.json`` per host).

Three templates:
  A  standard questions only                       -> Ready for a persona who answered them
  B  A + a required "why us?" essay                -> Essay
  C  A + an unknown question + a new wording       -> Needs answers
"""

from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "overlay"
PAGES = HERE / "pages"
JS = HERE / "js"
BASE = HERE.parent / "sources" / "base"

GH_JOBS = {
    "northwind": {"1001": "A", "1002": "B", "1003": "A", "1004": "C", "1099": "A"},
    "ledgerline": {"2001": "A", "2002": "C", "2003": "B"},
    "tessellate": {"3001": "A", "3002": "B", "3003": "C"},
}
LEVER_JOBS = {
    "bluefin": {
        "4d1c2e7a-0b6f-4c1e-9a7d-2f3b5c6d7e01": "A",
        "4d1c2e7a-0b6f-4c1e-9a7d-2f3b5c6d7e02": "C",
    },
    "driftwood": {"7a8b9c0d-1e2f-4a3b-8c4d-5e6f7a8b9c01": "B"},
}
ASHBY_JOBS = {
    "quillstack": {
        "9e8d7c6b-5a49-4382-9170-6f5e4d3c2b01": "A",
        "9e8d7c6b-5a49-4382-9170-6f5e4d3c2b02": "C",
    },
    "kitewire": {"1f2e3d4c-5b6a-4798-8695-a4b3c2d1e001": "B"},
}
COMPANY = {
    "northwind": "Northwind Robotics",
    "ledgerline": "Ledgerline Financial",
    "tessellate": "Tessellate",
    "bluefin": "Bluefin Analytics",
    "driftwood": "Driftwood Labs",
    "quillstack": "Quillstack",
    "kitewire": "Kitewire",
}
HEARD = ["Job board", "Company website", "Referral", "University career center", "Other"]
ROBOTICS_1099 = (
    "Northwind Robotics is hiring a robotics software intern for summer 2027.\n"
    "Requirements:\n- Currently pursuing a degree in computer science or a related field\n"
    "- Experience with Python\n- Familiarity with C++ or ROS\n"
    "Nice to have:\n- Experience with Docker\n"
)


def yn(label: str, name: str, required: bool = True) -> dict:
    return {
        "required": required,
        "label": label,
        "description": None,
        "fields": [
            {
                "name": name,
                "type": "multi_value_single_select",
                "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}],
            }
        ],
    }


def gh_questions(board: str, jid: str, tpl: str) -> list[dict]:
    company = COMPANY[board]
    q = [
        {
            "required": True,
            "label": "First Name",
            "fields": [{"name": "first_name", "type": "input_text", "values": []}],
        },
        {
            "required": True,
            "label": "Last Name",
            "fields": [{"name": "last_name", "type": "input_text", "values": []}],
        },
        {
            "required": True,
            "label": "Email",
            "fields": [{"name": "email", "type": "input_text", "values": []}],
        },
        {
            "required": True,
            "label": "Phone",
            "fields": [{"name": "phone", "type": "input_text", "values": []}],
        },
        {
            "required": True,
            "label": "Resume/CV",
            "fields": [
                {"name": "resume", "type": "input_file", "values": []},
                {"name": "resume_text", "type": "textarea", "values": []},
            ],
        },
        {
            "required": False,
            "label": "Cover Letter",
            "fields": [
                {"name": "cover_letter", "type": "input_file", "values": []},
                {"name": "cover_letter_text", "type": "textarea", "values": []},
            ],
        },
        {
            "required": False,
            "label": "LinkedIn Profile",
            "fields": [{"name": f"question_{jid}01", "type": "input_text", "values": []}],
        },
        yn("Are you legally authorized to work in the United States?", f"question_{jid}02"),
        yn(
            "Will you now or in the future require sponsorship for employment visa status (e.g., H-1B visa status)?",
            f"question_{jid}03",
        ),
        {
            "required": True,
            "label": "How did you hear about this job?",
            "fields": [
                {
                    "name": f"question_{jid}04",
                    "type": "multi_value_single_select",
                    "values": [{"label": h, "value": i} for i, h in enumerate(HEARD)],
                }
            ],
        },
    ]
    if tpl == "B":
        q.append(
            {
                "required": True,
                "label": f"Why do you want to work at {company}?",
                "fields": [{"name": f"question_{jid}05", "type": "textarea", "values": []}],
            }
        )
    if tpl == "C":
        q.append(yn("Do you have a valid driver's license?", f"question_{jid}06"))
        q.append(
            yn(
                f"Will you require {company} to sponsor you for a work permit now or in the future for the location(s) you selected?",
                f"question_{jid}07",
            )
        )
    return q  # fmt: skip


GH_LOCATION = [
    {"required": True, "label": "Latitude", "fields": [{"name": "latitude", "type": "input_hidden", "values": []}]},
    {"required": True, "label": "Longitude", "fields": [{"name": "longitude", "type": "input_hidden", "values": []}]},
    {"required": True, "label": "Location (City)", "fields": [{"name": "location", "type": "input_text", "values": []}]},
]  # fmt: skip
LOCATIONS = [
    "Northfield, Illinois, United States", "Northfield, Minnesota, United States",
    "Lakeview, Massachusetts, United States", "Lakeview, Oregon, United States",
    "Coastal Plains, Massachusetts, United States", "Granite Valley, Colorado, United States",
    "Granite Valley, Idaho, United States", "Springfield, Illinois, United States",
]  # fmt: skip
STATE_ABBR = {
    "Illinois": "IL",
    "Minnesota": "MN",
    "Massachusetts": "MA",
    "Oregon": "OR",
    "Colorado": "CO",
    "Idaho": "ID",
}
PHONE_COUNTRIES = ["United States +1", "Canada +1", "India +91", "United Kingdom +44", "China +86"]
GH_UPLOAD = "https://grnhse-prod-jben-us-west-2.s3.us-west-2.amazonaws.com/"
ASHBY_UPLOAD = (
    "https://ashbyhq-infra-prd-main-app-uploaded-files-us-east-1.s3.us-east-1.amazonaws.com/"
)
PARSED = {
    "name": "Parsed Resume Name",
    "email": "parsed.resume@example.com",
    "phone": "617-555-0199",
    "org": "Parsed Company",
}


GH_COMPLIANCE = [
    {
        "type": "eeoc",
        "description": "&lt;p&gt;Voluntary self-identification (synthetic fixture).&lt;/p&gt;",
        "questions": [
            {"required": False, "label": "Gender", "fields": [{"name": "gender", "type": "multi_value_single_select", "values": [{"label": "Male", "value": 1}, {"label": "Female", "value": 2}, {"label": "Decline To Self Identify", "value": 3}]}]},
            {"required": False, "label": "Race", "fields": [{"name": "race", "type": "multi_value_single_select", "values": [{"label": "Asian", "value": 1}, {"label": "White", "value": 2}, {"label": "Decline To Self Identify", "value": 3}]}]},
            {"required": False, "label": "VeteranStatus", "fields": [{"name": "veteran_status", "type": "multi_value_single_select", "values": [{"label": "I am not a protected veteran", "value": 1}, {"label": "I don't wish to answer", "value": 3}]}]},
        ],
    }
]  # fmt: skip


def write_greenhouse() -> None:
    for board, jobs in GH_JOBS.items():
        listing = BASE / f"boards-api.greenhouse.io/v1/boards/{board}/jobs.json"
        listed = {str(j["id"]): j for j in json.loads(listing.read_text())["jobs"]}
        for jid, tpl in jobs.items():
            single = BASE / f"boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}.json"
            if single.exists():
                job = json.loads(single.read_text())
            elif jid in listed:
                job = dict(listed[jid])
            else:  # listed on Simplify only, with its own posting text on the ATS
                job = {
                    "id": int(jid),
                    "title": "Robotics Intern",
                    "company_name": COMPANY[board],
                    "absolute_url": f"https://job-boards.greenhouse.io/{board}/jobs/{jid}",
                    "location": {"name": "Pittsburgh, PA"},
                    "first_published": "2026-10-10T12:00:00-04:00",
                    "content": html.escape(
                        "".join(f"<p>{ln}</p>" for ln in ROBOTICS_1099.splitlines())
                    ),
                }
            job["questions"] = gh_questions(board, jid, tpl)
            job["location_questions"] = GH_LOCATION if tpl == "A" else []
            job["compliance"] = GH_COMPLIANCE
            job["demographic_questions"] = None
            job["data_compliance"] = [{"type": "gdpr", "requires_consent": False}]
            out = OUT / f"boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(job, indent=1) + "\n")


def _li(label: str, inner: str, required: bool) -> str:
    star = '<span class="required">✱</span>' if required else ""
    return (
        f'<li class="application-question"><label><div class="application-label">{label}{star}'
        f'</div><div class="application-field">{inner}</div></label></li>'
    )


def _card(card_id: str, title: str, fields: list[dict]) -> str:
    tpl = html.escape(json.dumps({"text": title, "fields": fields, "id": card_id}), quote=True)
    items = []
    for i, f in enumerate(fields):
        name = f"cards[{card_id}][field{i}]"
        star = '<span class="required">✱</span>' if f["required"] else ""
        req = " required" if f["required"] else ""
        if f["type"] == "multiple-choice":
            inner = (
                '<ul data-qa="multiple-choice">'
                + "".join(
                    f'<li><label><input type="radio" name="{name}" value="{html.escape(o["text"])}"{req} />'
                    f'<span class="application-answer-alternative">{html.escape(o["text"])}</span></label></li>'
                    for o in f["options"]
                )
                + "</ul>"
            )
        elif f["type"] == "dropdown":
            inner = (
                f'<div class="application-dropdown"><select name="{name}"{req}><option value="">Select...</option>'
                + "".join(
                    f'<option value="{html.escape(o["text"])}">{html.escape(o["text"])}</option>'
                    for o in f["options"]
                )
                + "</select></div>"
            )
        elif f["type"] == "textarea":
            inner = f'<textarea class="card-field-input" name="{name}"{req}></textarea>'
        else:
            inner = f'<input class="card-field-input" type="text" name="{name}"{req}>'
        items.append(
            f'<li class="application-question custom-question"><div><div class="application-label '
            f'full-width {f["type"]}"><div class="text">{html.escape(f["text"])}{star}</div></div>'
            f'<div class="application-field full-width">{inner}</div></div></li>'
        )
    return (
        f'<div class="section page-centered application-form" data-qa="additional-cards"><h4>{title}</h4>'
        f'<input type="hidden" value="{tpl}" name="cards[{card_id}][baseTemplate]"><ul>{"".join(items)}</ul></div>'
    )  # fmt: skip


def lever_page(site: str, jid: str, tpl: str) -> str:
    company = COMPANY[site]
    yes_no = [{"text": "Yes"}, {"text": "No"}]
    fields = [
        {
            "type": "multiple-choice",
            "text": "Are you legally authorized to work in the country for which you are applying?",
            "required": True,
            "options": yes_no,
        },
        {
            "type": "multiple-choice",
            "text": "Will you now or in the future require sponsorship for employment visa status (e.g., H-1B, etc.)?",
            "required": True,
            "options": yes_no,
        },
        {
            "type": "dropdown",
            "text": "Please tell us how you heard about this opportunity.",
            "required": True,
            "options": [{"text": h} for h in HEARD],
        },
    ]
    if tpl == "B":
        fields.append(
            {"type": "textarea", "text": f"Why do you want to work at {company}?", "required": True}
        )
    if tpl == "C":
        fields.append(
            {
                "type": "multiple-choice",
                "text": "Do you have a valid driver's license?",
                "required": True,
                "options": yes_no,
            }
        )
    std = "".join([
        _li("Resume/CV", '<a href="#" class="visible-resume-upload"><span class="filename"></span><input class="application-file-input invisible-resume-upload" id="resume-upload-input" name="resume" type="file"></a>', True),
        _li("Full name", '<input type="text" name="name" required>', True),
        _li("Email", '<input name="email" type="email" required>', True),
        _li("Phone", '<input type="text" name="phone">', False),
        _li("Current location", '<input class="location-input" id="location-input" type="text" maxlength="100" name="location" required><input id="selected-location" type="hidden" name="selectedLocation"><div class="dropdown-container" style="display: none;"><div class="dropdown-results"></div></div>', True),
        _li("Current company", '<input type="text" name="org">', False),
        _li("LinkedIn URL", '<input type="text" name="urls[LinkedIn]">', False),
        _li("GitHub URL", '<input type="text" name="urls[GitHub]">', False),
    ])  # fmt: skip
    return (
        f"<!doctype html><html><head><title>{company} - Apply</title></head><body>"
        f'<form id="application-form" enctype="multipart/form-data" method="POST">'
        f'<div class="section page-centered application-form"><h4>Submit your application</h4><ul>{std}</ul></div>'
        f"{_card(jid[:8] + '-card', 'Questions', fields)}"
        f'<div class="application-additional"><textarea name="comments" placeholder="Add a cover letter or anything else you want to share."></textarea></div>'
        f'<input type="hidden" name="h-captcha-response" value=""><div id="h-captcha" class="h-captcha"></div>'
        f'<button id="btn-submit" type="button" class="template-btn-submit">Submit application</button>'
        f"</form><script>{(JS / 'lever.js').read_text()}</script></body></html>\n"
    )  # fmt: skip


def write_lever() -> None:
    for site, jobs in LEVER_JOBS.items():
        for jid, tpl in jobs.items():
            out = OUT / f"jobs.lever.co/{site}/{jid}/apply.html"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(lever_page(site, jid, tpl))


def _fe(
    path: str, title: str, ftype: str, required: bool, options: list[str] | None = None
) -> dict:
    f = {"id": path, "path": path, "title": title, "type": ftype, "isNullable": not required}
    if options:
        f["selectableValues"] = [{"label": o, "value": o} for o in options]
    return {
        "id": f"entry_{path}",
        "field": f,
        "isRequired": required,
        "descriptionHtml": None,
        "isHidden": None,
    }


def ashby_posting(board: str, jid: str, tpl: str) -> dict:
    company = COMPANY[board]
    entries = [
        _fe("_systemfield_name", "Name", "String", True),
        _fe("_systemfield_email", "Email", "Email", True),
        _fe(f"{jid[:8]}-phone", "Phone", "Phone", False),
        _fe("_systemfield_resume", "Resume", "File", True),
        _fe(f"{jid[:8]}-linkedin", "LinkedIn Profile", "String", False),
        _fe(
            f"{jid[:8]}-auth",
            "Are you legally authorized to work in the United States?",
            "Boolean",
            True,
        ),
        _fe(
            f"{jid[:8]}-spons",
            "Will you now or in the future require visa sponsorship?",
            "Boolean",
            True,
        ),
        _fe(
            f"{jid[:8]}-heard",
            "How did you hear about this opportunity?",
            "ValueSelect",
            False,
            HEARD,
        ),
    ]
    if tpl == "B":
        entries.append(
            _fe(f"{jid[:8]}-why", f"Why do you want to work at {company}?", "LongText", True)
        )
    if tpl == "C":
        entries.append(
            _fe(f"{jid[:8]}-lic", "Do you have a valid driver's license?", "Boolean", True)
        )
    eeo = [
        _fe(
            "_systemfield_eeoc_gender",
            "Gender",
            "ValueSelect",
            False,
            ["Male", "Female", "Decline to self-identify"],
        ),
        _fe(
            "_systemfield_eeoc_veteran_status",
            "Veteran Status",
            "ValueSelect",
            False,
            [
                "I am not a protected veteran",
                "I decline to self-identify for protected veteran status",
            ],
        ),
    ]
    return {
        "jobPosting": {
            "id": jid,
            "title": "Synthetic posting",
            "descriptionHtml": f"<p>{company} synthetic posting.</p><p>Requirements:</p><ul><li>Experience with Python</li></ul>",
            "applicationForm": {"sections": [{"title": None, "fieldEntries": entries}]},
            "surveyForms": [{"sections": [{"descriptionHtml": "<p>U.S. EQUAL EMPLOYMENT OPPORTUNITY INFORMATION (voluntary)</p>", "fieldEntries": eeo}]}],
            "shouldAskForTextingConsent": False,
        }
    }  # fmt: skip


def write_ashby() -> None:
    for board, jobs in ASHBY_JOBS.items():
        for jid, tpl in jobs.items():
            out = OUT / f"jobs.ashbyhq.com/{board}/{jid}/form.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(ashby_posting(board, jid, tpl), indent=1) + "\n")


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _gh_label(fid: str, label: str, required: bool, cls: str = "label") -> str:
    star = '<span aria-hidden="true">*</span>' if required else ""
    return f'<label id="{fid}-label" for="{fid}" class="{cls}">{_esc(label)}{star}</label>'


def _gh_select(
    fid: str,
    label: str,
    required: bool,
    options: list[str],
    *,
    multi: bool = False,
    kind: str = "select",
) -> str:
    return (
        f'<div class="select__container" data-kind="{kind}" data-multi="{1 if multi else 0}" data-options="{_esc(json.dumps(options))}">'
        f"{_gh_label(fid, label, required, 'label select__label')}"
        '<div class="select-shell"><div class="select__control"><div class="select__value-container">'
        f'<div class="select__input-container"><input class="select__input" id="{fid}" type="text" role="combobox" '
        f'aria-autocomplete="list" aria-expanded="false" aria-labelledby="{fid}-label" aria-required="{str(required).lower()}" autocomplete="off"></div>'
        '</div><div class="select__indicators"><button type="button" class="icon-button" aria-label="Toggle flyout"></button></div></div></div></div>'
    )  # fmt: skip


def _gh_field(q: dict) -> str:
    fields = [f for f in q.get("fields") or [] if f.get("type") != "input_hidden"]
    if not fields:
        return ""
    f, label, req = fields[0], q["label"], bool(q.get("required"))
    name, ftype = f["name"], f["type"]
    if name == "phone":
        return (
            '<fieldset class="phone-input"><legend class="visually-hidden">Phone</legend>'
            + _gh_select("country", "Country", False, PHONE_COUNTRIES, kind="phone-country")
            + '<button type="button" class="iti__selected-country" title="" aria-label="Select country"></button>'
            + f'<div class="field-wrapper">{_gh_label("phone", label, req)}<input id="phone" class="input iti__tel-input" aria-label="Phone" type="tel" maxlength="255"></div></fieldset>'
        )
    if name == "location":
        return _gh_select("candidate-location", label, req, [], kind="location")
    if ftype == "input_file":
        star = '<span aria-hidden="true">*</span>' if req else ""
        return (
            f'<div class="file-upload"><div id="upload-label-{name}" class="label upload-label">{_esc(label)}{star}</div>'
            f'<div class="file-upload__wrapper"><button type="button" class="btn btn--pill">Attach</button>'
            f'<label class="visually-hidden" for="{name}">Attach</label><input id="{name}" class="visually-hidden" type="file" accept=".pdf,.doc,.docx,.txt,.rtf"></div></div>'
        )
    if ftype in ("multi_value_single_select", "multi_value_multi_select"):
        opts = [str(v["label"]) for v in f.get("values") or []]
        return _gh_select(name, label, req, opts, multi=ftype == "multi_value_multi_select")
    if ftype == "textarea":
        return f'<div class="field-wrapper">{_gh_label(name, label, req)}<textarea id="{name}" class="input input__multi-line" aria-required="{str(req).lower()}"></textarea></div>'
    return f'<div class="field-wrapper">{_gh_label(name, label, req)}<input id="{name}" class="input input__single-line" aria-label="{_esc(label)}" aria-required="{str(req).lower()}" type="text" maxlength="255"></div>'


def gh_page(job: dict) -> str:
    parts = [_gh_field(q) for q in job["questions"]]
    parts += [_gh_field(q) for q in job.get("location_questions") or []]
    eeo = [
        _gh_field(q) for block in job.get("compliance") or [] for q in block.get("questions") or []
    ]
    return (
        f"<!doctype html><html><head><meta charset=\"utf-8\"><title>Job Application for {_esc(job['title'])} at {_esc(job['company_name'])}</title></head>"
        f'<body data-upload-url="{GH_UPLOAD}"><main><h1>{_esc(job["title"])}</h1>'
        f'<form method="get" id="application-form" class="application--form">{"".join(parts)}'
        f'<div class="eeoc__container"><h3>Voluntary Self-Identification</h3>{"".join(eeo)}</div>'
        f'<button type="submit" class="btn btn--pill">Submit application</button><p id="fx-status"></p></form></main>'
        f'<script type="application/json" id="fx-locations">{json.dumps(LOCATIONS)}</script>'
        f"<script>{(JS / 'greenhouse.js').read_text()}</script></body></html>\n"
    )  # fmt: skip


def _ab_entry(fe: dict) -> str:
    f = fe["field"]
    path, title, ftype, req = f["path"], f["title"], f["type"], fe["isRequired"]
    star = " _required" if req else ""
    head = f'<label for="{path}" class="ashby-application-form-question-title{star}">{_esc(title)}</label>'
    opts = [v["label"] for v in f.get("selectableValues") or []]
    eid = f"entry_{path}"
    if ftype == "Boolean":
        inner = (
            '<div class="yesno">'
            '<button class="ashby-application-form-input-yesno-option" aria-pressed="false" data-option="yes">Yes</button>'
            '<button class="ashby-application-form-input-yesno-option" aria-pressed="false" data-option="no">No</button>'
            f'<input type="checkbox" name="{path}" tabindex="-1" style="display:none"></div>'
        )
    elif ftype in ("ValueSelect", "MultiValueSelect"):
        kind = "radio" if ftype == "ValueSelect" else "checkbox"
        inner = "".join(
            f'<div class="_option"><span><input type="{kind}" id="{eid}-labeled-{kind}-{i}" name="{eid if kind == "radio" else _esc(o)}"></span>'
            f'<label for="{eid}-labeled-{kind}-{i}">{_esc(o)}</label></div>'
            for i, o in enumerate(opts)
        )
        return f'<div data-field-path="{path}"><fieldset class="ashby-application-form-field-entry">{head}{inner}</fieldset></div>'
    elif ftype == "File":
        inner = f'<input type="file" id="{path}" accept="application/pdf,.pdf,.doc,.docx,text/plain,.txt,text/markdown,.md"><div class="fx-file-name"></div>'
    elif ftype == "Location":
        return (
            f'<div class="ashby-application-form-field-entry fx-location" data-field-path="{path}">{head}'
            '<input class="ashby-application-form-input-autocomplete" placeholder="Start typing..." aria-autocomplete="list" role="combobox">'
            '<div role="listbox"></div></div>'
        )
    elif ftype == "LongText":
        inner = f'<textarea id="{path}" name="{path}" placeholder="Type here..."></textarea>'
    else:
        itype = {"Email": "email", "Phone": "tel"}.get(ftype, "text")
        inner = f'<input id="{path}" name="{path}" type="{itype}" placeholder="Type here...">'
    return f'<div class="ashby-application-form-field-entry" data-field-path="{path}">{head}{inner}</div>'


def ashby_page(posting: dict) -> str:
    jp = posting["jobPosting"]
    entries = [fe for s in jp["applicationForm"]["sections"] for fe in s["fieldEntries"]]
    survey = [
        fe for sf in jp.get("surveyForms") or [] for s in sf["sections"] for fe in s["fieldEntries"]
    ]
    return (
        f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{_esc(jp['title'])}</title></head>"
        f'<body data-upload-url="{ASHBY_UPLOAD}"><div class="ashby-application-form-container">'
        f'{"".join(_ab_entry(fe) for fe in entries)}'
        f'<div class="ashby-survey"><p>U.S. EQUAL EMPLOYMENT OPPORTUNITY INFORMATION (voluntary)</p>{"".join(_ab_entry(fe) for fe in survey)}</div>'
        '<button class="ashby-application-form-submit-button"><span>Submit Application</span></button><p id="fx-status"></p></div>'
        f"<script>{(JS / 'ashby.js').read_text()}</script></body></html>\n"
    )  # fmt: skip


def _endpoint(path: str, spec: dict) -> None:
    out = PAGES / path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, indent=1) + "\n")


def write_pages() -> None:
    for board, jobs in GH_JOBS.items():
        for jid in jobs:
            job = json.loads(
                (OUT / f"boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}.json").read_text()
            )
            out = PAGES / f"job-boards.greenhouse.io/{board}/jobs/{jid}.html"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(gh_page(job))
    for board, jobs in ASHBY_JOBS.items():
        for jid, tpl in jobs.items():
            out = PAGES / f"jobs.ashbyhq.com/{board}/{jid}/application.html"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(ashby_page(ashby_posting(board, jid, tpl)))
    host = GH_UPLOAD.split("/")[2]
    _endpoint(f"{host}/index.POST.json", {"status": 204})
    _endpoint("job-boards.greenhouse.io/_default.POST.json", {"json": {"ok": True}})
    lever_locs = [
        {
            "name": loc.replace(", United States", ", USA").replace(
                loc.split(", ")[1], STATE_ABBR[loc.split(", ")[1]]
            )
        }
        for loc in LOCATIONS
    ]
    _endpoint("jobs.lever.co/searchLocations.json", lever_locs)  # type: ignore[arg-type]
    _endpoint("jobs.lever.co/parseResume.POST.json", {"json": PARSED})
    _endpoint(
        "jobs.lever.co/_default.POST.json",
        {
            "content_type": "text/html",
            "body": "<!doctype html><h1>Application submitted!</h1><p>Thanks for applying.</p>",
        },
    )
    gql = "jobs.ashbyhq.com/api/non-user-graphql"
    _endpoint(
        f"{gql}/ApiAutocompleteGeoLocation.POST.json", {"json": {"data": {"result": LOCATIONS}}}
    )
    _endpoint(
        f"{gql}/ApiCreateFileUploadHandle.POST.json", {"json": {"data": {"handle": "fx-handle"}}}
    )
    _endpoint(
        f"{gql}/ApiAutofillApplicationFormWithUploadedResume.POST.json",
        {"json": {"data": {"autofill": PARSED}}},
    )
    _endpoint(
        f"{gql}/ApiSubmitSingleApplicationFormAction.POST.json",
        {"json": {"data": {"submitted": True}}},
    )
    _endpoint(f"{ASHBY_UPLOAD.split('/')[2]}/index.POST.json", {"status": 204})


def main() -> None:
    for d in (OUT, PAGES):
        if d.exists():
            shutil.rmtree(d)
    write_greenhouse()
    write_lever()
    write_ashby()
    write_pages()
    n = sum(1 for d in (OUT, PAGES) for f in d.rglob("*") if f.is_file())
    print(f"wrote {n} files to {OUT} and {PAGES}")


if __name__ == "__main__":
    main()
