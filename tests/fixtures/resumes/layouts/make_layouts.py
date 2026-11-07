"""Regenerate the resume-layout corpus (synthetic people, many layouts):

    uv run python tests/fixtures/resumes/layouts/make_layouts.py

Real resumes come in layouts that text extraction handles badly. Each person here is written in
several of them, and ``expected.json`` records what a parser should read from every file:

  pdf_plain      one column, Chrome's PDF text layer
  pdf_latex      a LaTeX-style resume: centered name, "a | b | c" contact line, small-caps
                 letter-spaced section headings, dates right-aligned on the title line,
                 ligatures (fi, ffi), italic sub-lines
  pdf_twocol     a sidebar (contact, skills, languages, education) beside the experience
  pdf_scanned    an image only: no text layer at all (needs OCR)
  docx_plain     paragraphs and Word bullet lists
  docx_table     the whole resume laid out in a two-column table
  docx_header    name and contact details in the Word page header, the rest in the body
  md             Markdown

PDFs are printed by Chrome through Playwright (dev only: the tests read the committed files).
Every name, email, phone, school and company is fictional; emails use reserved domains.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import docx
from docx.shared import Pt

HERE = Path(__file__).resolve().parent

PEOPLE = {
    "avery": {
        "name": "Avery Testwell",
        "email": "avery.testwell@example.com",
        "school_email": "avery@northfield.edu.example",
        "phone": "(312) 555-0142",
        "location": "Evanston, IL",
        "linkedin": "linkedin.com/in/avery-testwell-example",
        "github": "github.com/avery-testwell-example",
        "education": [
            {
                "school": "Northfield State University",
                "degree": "B.S. in Computer Science",
                "dates": "Sep 2024 – May 2028",
                "gpa": "GPA: 3.82/4.00",
                "detail": "Relevant coursework: Data Structures, Algorithms, Operating Systems",
            },
        ],
        "experience": [
            {
                "title": "Software Engineering Intern",
                "org": "Lakeshore Logistics",
                "place": "Chicago, IL",
                "dates": "Jun 2025 – Aug 2025",
                "bullets": [
                    "Built an efficient route-planning service in Python used by 40 dispatchers",
                    "Certified the officewide deployment checklist with the platform team",
                ],
            },
            {
                "title": "Teaching Assistant",
                "org": "Northfield State University",
                "place": "Evanston, IL",
                "dates": "Jan 2025 – Present",
                "bullets": ["Run weekly office hours for 120 students in Intro to Programming"],
            },
        ],
        "skills": [
            ("Languages", "Python, Java, C++, SQL"),
            ("Frameworks", "Flask, React, pytest"),
            ("Spoken Languages", "English, Spanish (Professional), Mandarin"),
        ],
    },
    "jordan": {
        "name": "Jordan Mockley",
        "email": "jordan.mockley@example.org",
        "school_email": None,
        "phone": "+1 206-555-0177",
        "location": "Seattle, WA",
        "linkedin": "linkedin.com/in/jordan-mockley-example",
        "github": None,
        "education": [
            {
                "school": "Cascade Institute of Technology",
                "degree": "M.S. in Data Science",
                "dates": "Sep 2025 – Dec 2026",
                "gpa": "GPA 3.9",
                "detail": "Thesis: forecasting regional transit ridership",
            },
            {
                "school": "Riverbend College",
                "degree": "B.A. in Economics",
                "dates": "Sep 2019 – May 2023",
                "gpa": "",
                "detail": "",
            },
        ],
        "experience": [
            {
                "title": "Data Analyst",
                "org": "Harborview Analytics",
                "place": "Seattle, WA",
                "dates": "Jul 2023 – Aug 2025",
                "bullets": [
                    "Owned the weekly retention dashboard for the finance office",
                    "Cut report runtime from 40 to 6 minutes by rewriting SQL",
                ],
            },
        ],
        "skills": [
            ("Technical", "Python, R, SQL, dbt, Tableau"),
            ("Languages", "English (native), Hindi"),
        ],
    },
}

# What a parser should read from each person (independent of layout).
EXPECTED = {
    "avery": {
        "name": "Avery Testwell",
        "email": "avery.testwell@example.com",
        "phone": "3125550142",
        "linkedin": "linkedin.com/in/avery-testwell-example",
        "github": "github.com/avery-testwell-example",
        "city": "Evanston",
        "region": "IL",
        "school_email": "avery@northfield.edu.example",
        "gpa": 3.82,
        "gpa_scale": 4.0,
        "school": "Northfield State University",
        "graduation": "2028-05",
        "employers": ["Lakeshore Logistics", "Northfield State University"],
        "spoken_languages": ["English", "Spanish", "Mandarin"],
    },
    "jordan": {
        "name": "Jordan Mockley",
        "email": "jordan.mockley@example.org",
        "phone": "2065550177",
        "linkedin": "linkedin.com/in/jordan-mockley-example",
        "github": None,
        "city": "Seattle",
        "region": "WA",
        "school_email": None,
        "gpa": 3.9,
        "gpa_scale": None,
        "school": "Cascade Institute of Technology",
        "graduation": "2026-12",
        "employers": ["Harborview Analytics"],
        "spoken_languages": ["English", "Hindi"],
    },
}

e = html.escape


def contact_bits(p: dict) -> list[str]:
    return [x for x in (p["phone"], p["email"], p["school_email"], p["linkedin"], p["github"])
            if x]  # fmt: skip


# --- HTML layouts (printed to PDF) --------------------------------------------------------------

BASE_CSS = "body{font:11pt/1.35 Helvetica,Arial,sans-serif;margin:0;color:#111}"


def html_plain(p: dict) -> str:
    out = [f"<h1 style='margin:0;font-size:20pt'>{e(p['name'])}</h1>",
           f"<p>{e(p['location'])} · {' · '.join(e(b) for b in contact_bits(p))}</p>",
           "<h2>Education</h2>"]  # fmt: skip
    for ed in p["education"]:
        out.append(f"<p><b>{e(ed['school'])}</b> — {e(ed['degree'])}, {e(ed['dates'])}<br>"
                   f"{e(ed['gpa'])} {e(ed['detail'])}</p>")  # fmt: skip
    out.append("<h2>Experience</h2>")
    for x in p["experience"]:
        out.append(f"<p><b>{e(x['title'])}</b>, {e(x['org'])} — {e(x['place'])} "
                   f"({e(x['dates'])})</p><ul>"
                   + "".join(f"<li>{e(b)}</li>" for b in x["bullets"]) + "</ul>")  # fmt: skip
    out.append("<h2>Skills</h2>")
    out += [f"<p><b>{e(k)}:</b> {e(v)}</p>" for k, v in p["skills"]]
    return f"<style>{BASE_CSS}</style><div style='padding:0'>{''.join(out)}</div>"


def html_latex(p: dict) -> str:
    css = """body{font:10.5pt/1.3 'Latin Modern Roman','CMU Serif','Times New Roman',Times,serif;
      font-variant-ligatures:common-ligatures;margin:0}
      .name{text-align:center;font-size:24pt;font-variant:small-caps;margin:0}
      .contact{text-align:center;font-size:9.5pt}
      h2{font-variant:small-caps;letter-spacing:.18em;font-size:12pt;font-weight:normal;
         border-bottom:1px solid #000;margin:12px 0 4px}
      .row{display:flex;justify-content:space-between}.sub{font-style:italic;font-size:9.5pt}
      ul{margin:2px 0 6px 18px;padding:0}li{font-size:9.5pt}"""
    out = [f"<p class=name>{e(p['name'])}</p>",
           f"<p class=contact>{' | '.join(e(b) for b in contact_bits(p))} | {e(p['location'])}</p>",
           "<h2>Education</h2>"]  # fmt: skip
    for ed in p["education"]:
        out.append(f"<div class=row><b>{e(ed['school'])}</b><span>{e(ed['dates'])}</span></div>"
                   f"<div class='row sub'><span>{e(ed['degree'])}</span><span>{e(ed['gpa'])}</span>"
                   "</div>")  # fmt: skip
    out.append("<h2>Experience</h2>")
    for x in p["experience"]:
        out.append(
            f"<div class=row><b>{e(x['title'])}</b><span>{e(x['dates'])}</span></div>"
            f"<div class='row sub'><span>{e(x['org'])}</span><span>{e(x['place'])}</span>"
            "</div><ul>" + "".join(f"<li>{e(b)}</li>" for b in x["bullets"]) + "</ul>"
        )
    out.append("<h2>Technical Skills</h2>")
    out += [f"<div><b>{e(k)}</b>: {e(v)}</div>" for k, v in p["skills"]]
    return f"<style>{css}</style>{''.join(out)}"


def html_twocol(p: dict) -> str:
    css = (
        BASE_CSS
        + """.wrap{display:grid;grid-template-columns:34% 1fr;gap:22px}
      .side{background:#eef2f7;padding:14px;font-size:9.5pt}h2{font-size:12pt;margin:10px 0 4px}"""
    )
    side = [
        f"<h1 style='font-size:18pt;margin:0 0 6px'>{e(p['name'])}</h1>",
        f"<div>{e(p['location'])}</div>",
    ] + [f"<div>{e(b)}</div>" for b in contact_bits(p)]
    side.append("<h2>Education</h2>")
    for ed in p["education"]:
        side.append(f"<div><b>{e(ed['school'])}</b></div><div>{e(ed['degree'])}</div>"
                    f"<div>{e(ed['dates'])}</div><div>{e(ed['gpa'])}</div>")  # fmt: skip
    side.append("<h2>Skills</h2>")
    side += [f"<div><b>{e(k)}:</b> {e(v)}</div>" for k, v in p["skills"]]
    main = ["<h2>Experience</h2>"]
    for x in p["experience"]:
        main.append(f"<div><b>{e(x['title'])}</b> — {e(x['org'])}, {e(x['place'])}</div>"
                    f"<div>{e(x['dates'])}</div><ul>"
                    + "".join(f"<li>{e(b)}</li>" for b in x["bullets"]) + "</ul>")  # fmt: skip
    return (f"<style>{css}</style><div class=wrap><div class=side>{''.join(side)}</div>"
            f"<div>{''.join(main)}</div></div>")  # fmt: skip


# --- DOCX layouts ------------------------------------------------------------------------------


def _bullets(d, items: list[str]) -> None:
    for b in items:
        d.add_paragraph(b, style="List Bullet")


def docx_plain(p: dict, path: Path) -> None:
    d = docx.Document()
    d.add_heading(p["name"], level=0)
    d.add_paragraph(f"{p['location']} | " + " | ".join(contact_bits(p)))
    d.add_heading("Education", level=1)
    for ed in p["education"]:
        d.add_paragraph(f"{ed['school']} — {ed['degree']} ({ed['dates']})")
        if ed["gpa"] or ed["detail"]:
            d.add_paragraph(f"{ed['gpa']} {ed['detail']}".strip())
    d.add_heading("Experience", level=1)
    for x in p["experience"]:
        d.add_paragraph(f"{x['title']}, {x['org']} — {x['place']} ({x['dates']})")
        _bullets(d, x["bullets"])
    d.add_heading("Skills", level=1)
    for k, v in p["skills"]:
        d.add_paragraph(f"{k}: {v}")
    d.save(str(path))


def docx_table(p: dict, path: Path) -> None:
    d = docx.Document()
    t = d.add_table(rows=1, cols=2)
    left, right = t.rows[0].cells
    left.text = p["name"]
    for line in [p["location"], *contact_bits(p), "Education"]:
        left.add_paragraph(line)
    for ed in p["education"]:
        for line in (ed["school"], ed["degree"], ed["dates"], ed["gpa"]):
            if line:
                left.add_paragraph(line)
    left.add_paragraph("Skills")
    for k, v in p["skills"]:
        left.add_paragraph(f"{k}: {v}")
    right.text = "Experience"
    for x in p["experience"]:
        right.add_paragraph(f"{x['title']}, {x['org']} — {x['place']} ({x['dates']})")
        for b in x["bullets"]:
            right.add_paragraph(f"• {b}")
    d.save(str(path))


def docx_header(p: dict, path: Path) -> None:
    d = docx.Document()
    hdr = d.sections[0].header
    hp = hdr.paragraphs[0]
    run = hp.add_run(p["name"])
    run.bold, run.font.size = True, Pt(18)
    hdr.add_paragraph(f"{p['location']} | " + " | ".join(contact_bits(p)))
    d.add_heading("Education", level=1)
    for ed in p["education"]:
        d.add_paragraph(f"{ed['school']} — {ed['degree']} ({ed['dates']}) {ed['gpa']}".strip())
    d.add_heading("Experience", level=1)
    for x in p["experience"]:
        d.add_paragraph(f"{x['title']}, {x['org']} — {x['place']} ({x['dates']})")
        _bullets(d, x["bullets"])
    d.add_heading("Skills", level=1)
    for k, v in p["skills"]:
        d.add_paragraph(f"{k}: {v}")
    d.save(str(path))


def markdown(p: dict) -> str:
    out = [f"# {p['name']}", "", f"{p['location']} · " + " · ".join(contact_bits(p)), "",
           "## Education", ""]  # fmt: skip
    for ed in p["education"]:
        out += [f"**{ed['school']}** — {ed['degree']}, {ed['dates']}",
                f"{ed['gpa']} {ed['detail']}".strip(), ""]  # fmt: skip
    out += ["## Experience", ""]
    for x in p["experience"]:
        out.append(f"**{x['org']}** — {x['title']}, {x['dates']}")
        out += [f"- {b}" for b in x["bullets"]] + [""]
    out += ["## Skills", ""] + [f"{k}: {v}" for k, v in p["skills"]]
    return "\n".join(out) + "\n"


def main() -> None:
    from playwright.sync_api import sync_playwright

    files: dict[str, dict] = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, channel="chrome")
        pg = b.new_page()
        for who, p in PEOPLE.items():
            for kind, build in (("pdf_plain", html_plain), ("pdf_latex", html_latex),
                                ("pdf_twocol", html_twocol)):  # fmt: skip
                pg.set_content(build(p))
                out = HERE / f"{who}-{kind}.pdf"
                pg.pdf(
                    path=str(out),
                    format="Letter",
                    margin={"top": "0.5in", "bottom": "0.5in", "left": "0.6in", "right": "0.6in"},
                )
                files[out.name] = {"person": who, "layout": kind}
            # scanned: a screenshot of the plain layout, printed as an image-only PDF
            pg.set_viewport_size({"width": 850, "height": 1100})
            pg.set_content(html_plain(p) + "<style>body{padding:40px}</style>")
            png = pg.screenshot(full_page=True)
            import base64

            img = base64.b64encode(png).decode()
            pg.set_content(f"<style>body{{margin:0}}img{{width:100%}}</style>"
                           f"<img src='data:image/png;base64,{img}'>")  # fmt: skip
            out = HERE / f"{who}-pdf_scanned.pdf"
            pg.pdf(
                path=str(out),
                format="Letter",
                margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            )
            files[out.name] = {"person": who, "layout": "pdf_scanned"}
        b.close()
    for who, p in PEOPLE.items():
        for kind, fn in (("docx_plain", docx_plain), ("docx_table", docx_table),
                         ("docx_header", docx_header)):  # fmt: skip
            out = HERE / f"{who}-{kind}.docx"
            fn(p, out)
            files[out.name] = {"person": who, "layout": kind}
        out = HERE / f"{who}-md.md"
        out.write_text(markdown(p), encoding="utf-8")
        files[out.name] = {"person": who, "layout": "md"}
    (HERE / "expected.json").write_text(
        json.dumps({"files": files, "people": EXPECTED}, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(files)} files")


if __name__ == "__main__":
    main()
