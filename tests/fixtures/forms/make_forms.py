"""Regenerate the offline application-form fixtures:

    uv run python tests/fixtures/forms/make_forms.py

Every company, form and question here is synthetic; the shapes follow what each ATS serves
(Greenhouse ``jobs/{id}?questions=true`` JSON, Lever's server-rendered ``/apply`` page with card
templates, the ``jobPosting`` object an Ashby application page loads). ``overlay/`` is laid over
``tests/fixtures/sources/base`` (it replaces the Greenhouse job files with question-bearing ones).

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
            job["location_questions"] = []
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
        _li("Resume/CV", '<input class="application-file-input" name="resume" type="file">', True),
        _li("Full name", '<input type="text" name="name" required>', True),
        _li("Email", '<input name="email" type="email" required>', True),
        _li("Phone", '<input type="text" name="phone">', False),
        _li("Current location", '<input class="location-input" type="text" maxlength="100" name="location" required>', True),
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
        f"</form></body></html>\n"
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


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    write_greenhouse()
    write_lever()
    write_ashby()
    print(f"wrote {sum(1 for _ in OUT.rglob('*') if _.is_file())} files to {OUT}")


if __name__ == "__main__":
    main()
