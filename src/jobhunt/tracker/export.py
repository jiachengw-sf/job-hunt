"""`jobhunt apps export`: every application as rows with readable column names."""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any

from jobhunt.paths import secure_file

EXPORT_COLUMNS: list[tuple[str, str]] = [
    ("Company", "company"),
    ("Position", "position"),
    ("Team", "team"),
    ("Location", "location"),
    ("Type", "employment"),
    ("Pay (as posted)", "pay"),
    ("Applied", "applied"),
    ("Method", "method"),
    ("Status", "status"),
    ("Last update", "last_update"),
    ("Days since applied", "days_since_applied"),
    ("Where to check", "where_to_check"),
    ("How to check", "status_check_note"),
    ("Sign-in email", "login_email"),
    ("Next step", "next_step"),
    ("Follow-up due", "followup_due_on"),
    ("Interviews", "interviews"),
    ("Needs attention", "attention"),
    ("Confirmation ID", "confirmation_id"),
    ("Referral", "referral_contact"),
    ("Recruiter", "recruiter"),
    ("Outcome", "outcome"),
    ("Notes", "notes"),
    ("Resume", "resume_file"),
    ("Posting URL", "posting_url"),
    ("Application ID", "app_id"),
]


def export_rows(views: list[dict[str, Any]]) -> list[list[str]]:
    rows = [[h for h, _ in EXPORT_COLUMNS]]
    for v in views:
        v = v | {
            "interviews": "; ".join(d["when"] for d in v["upcoming_interviews"]),
            "attention": "; ".join(v["attention"]),
        }
        rows.append([str(v[k]) if v[k] not in (None, "") else "" for _, k in EXPORT_COLUMNS])
    return rows


def write_csv(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    # utf-8-sig so Excel opens accented names correctly
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)
    os.replace(tmp, path)
    secure_file(path)


def write_xlsx(path: Path, rows: list[list[str]]) -> None:
    from openpyxl import Workbook

    from jobhunt.tracker.backends import XlsxGrid

    wb = Workbook()
    ws = wb.active
    ws.title = "Applications"
    grid = XlsxGrid(ws, [])
    grid.append_rows(rows)
    grid.format_table(status_col=rows[0].index("Status") + 1, widths=_widths(rows))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}.tmp.xlsx")
    wb.save(tmp)
    os.replace(tmp, path)
    secure_file(path)


def _widths(rows: list[list[str]]) -> list[int]:
    return [min(60, max(10, *(len(r[i]) for r in rows[:200])) + 2) for i in range(len(rows[0]))]
