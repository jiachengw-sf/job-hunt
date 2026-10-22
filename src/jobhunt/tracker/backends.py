"""Spreadsheet backends behind one small grid interface: XLSX (openpyxl) and Google Sheets
(gspread, optional ``[sheets]`` extra). Sync works cell by cell, so a user's formatting, extra
columns and row order survive."""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from jobhunt.errors import ConfigError, JobhuntError
from jobhunt.install import reinstall_command


class TrackerLocked(JobhuntError):
    code = "tracker_locked"


class Grid(Protocol):
    def values(self) -> list[list[str]]: ...
    def set_cells(self, updates: list[tuple[int, int, str]]) -> None: ...  # 1-based row, col
    def append_rows(self, rows: list[list[str]]) -> None: ...
    def replace_all(self, rows: list[list[str]]) -> None: ...


class Backend(Protocol):
    name: str
    location: str

    def tab(self, title: str, *, user_columns: list[str] | None = None) -> Grid: ...
    def save(self) -> None: ...


def cell_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == datetime.min.time() else v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


# --- XLSX -----------------------------------------------------------------------------------

USER_FILL = "FFF4E5"  # light amber marks the columns you own
# status colors (light fills that keep dark text readable) and the overdue follow-up color
STATUS_FILLS = {
    "Prefilled": "EDEDED",
    "Applied": "DCEBFB",
    "OA": "E8DEF8",
    "Interviewing": "FFF0C2",
    "Offer": "D4EDDA",
    "Rejected": "F8D7DA",
    "Withdrawn": "E2E3E5",
    "Skipped": "E2E3E5",
}
OVERDUE_FILL = "F5B7B1"
FORMAT_ROWS = 2000  # dropdown and colors cover this many rows


def _col(n: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(n)


class XlsxGrid:
    def __init__(self, ws: Any, user_columns: list[str]) -> None:
        self.ws = ws
        self.user_columns = set(user_columns)

    def values(self) -> list[list[str]]:
        rows = [[cell_text(c) for c in r] for r in self.ws.iter_rows(values_only=True)]
        while rows and not any(rows[-1]):
            rows.pop()
        return rows

    def set_cells(self, updates: list[tuple[int, int, str]]) -> None:
        for r, c, v in updates:
            cell = self.ws.cell(row=r, column=c, value=v if v != "" else None)
            if r == 1:
                self._style_header(cell)

    def append_rows(self, rows: list[list[str]]) -> None:
        start = self.ws.max_row + 1 if self.values() else 1
        for i, row in enumerate(rows):
            for j, v in enumerate(row, start=1):
                cell = self.ws.cell(row=start + i, column=j, value=v if v != "" else None)
                if start + i == 1:
                    self._style_header(cell)

    def replace_all(self, rows: list[list[str]]) -> None:
        self.ws.delete_rows(1, self.ws.max_row)
        self.append_rows(rows)

    def format_table(
        self,
        *,
        status_col: int,
        widths: list[int],
        statuses: list[str] | None = None,
        due_col: int | None = None,
        link_col: int | None = None,
    ) -> None:
        """Frozen header, filters, column widths, a Status dropdown, status colors, overdue
        follow-ups in red and clickable links. Re-applying replaces jobhunt's own rules only."""
        from openpyxl.formatting.rule import FormulaRule
        from openpyxl.styles import Font, PatternFill
        from openpyxl.worksheet.datavalidation import DataValidation

        from jobhunt.tracker.states import machine

        ws = self.ws
        ws.freeze_panes = "A2"
        last = _col(max(len(widths), 1))
        ws.auto_filter.ref = f"A1:{last}{max(ws.max_row, 2)}"
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[_col(i)].width = w
        for c in ws[1]:
            self._style_header(c)
        sc = _col(status_col)
        rng = f"{sc}2:{sc}{FORMAT_ROWS}"
        names = statuses or list(machine().states)
        ws.data_validations.dataValidation = [
            dv for dv in ws.data_validations.dataValidation if dv.sqref != rng
        ]
        dv = DataValidation(type="list", formula1='"' + ",".join(names) + '"', allow_blank=True)
        dv.add(rng)
        ws.add_data_validation(dv)
        ws.conditional_formatting = type(ws.conditional_formatting)()
        whole = f"A2:{last}{FORMAT_ROWS}"
        if due_col:
            dc = _col(due_col)
            ws.conditional_formatting.add(
                f"{dc}2:{dc}{FORMAT_ROWS}",
                FormulaRule(
                    formula=[f'AND(${sc}2="Applied",${dc}2<>"",DATEVALUE(${dc}2)<=TODAY())'],
                    fill=PatternFill("solid", start_color=OVERDUE_FILL, end_color=OVERDUE_FILL),
                    font=Font(bold=True),
                    stopIfTrue=True,
                ),
            )
        for name, color in STATUS_FILLS.items():
            ws.conditional_formatting.add(
                whole,
                FormulaRule(
                    formula=[f'${sc}2="{name}"'],
                    fill=PatternFill("solid", start_color=color, end_color=color),
                ),
            )
        if link_col:
            for row in ws.iter_rows(min_row=2, min_col=link_col, max_col=link_col):
                cell = row[0]
                v = cell.value
                if isinstance(v, str) and v.startswith(("https://", "http://")):
                    cell.hyperlink = v
                    cell.font = Font(color="0563C1", underline="single")
                else:
                    cell.hyperlink = None
                    cell.font = Font()

    def _style_header(self, cell: Any) -> None:
        from openpyxl.styles import Font, PatternFill

        cell.font = Font(bold=True)
        if cell.value in self.user_columns:
            cell.fill = PatternFill("solid", start_color=USER_FILL, end_color=USER_FILL)


def xlsx_lock_reason(path: Path) -> str | None:
    """Why the workbook can't be written right now (open in Excel/LibreOffice), or None."""
    for lock in (path.parent / f"~${path.name}", path.parent / f".~lock.{path.name}#"):
        if lock.exists():
            return f"{path.name} is open in a spreadsheet app ({lock.name} exists)"
    if path.exists():
        try:
            with open(path, "r+b"):
                pass
        except PermissionError:
            return f"{path.name} is locked by another program"
    return None


class XlsxBackend:
    name = "xlsx"

    def __init__(self, path: Path) -> None:
        from openpyxl import Workbook, load_workbook

        self.path = path
        self.location = str(path)
        if reason := xlsx_lock_reason(path):
            raise TrackerLocked(
                f"Skipped the tracker sync: {reason}.",
                user_actions=[f"Close {path.name}, then run `jobhunt sync` again."],
            )
        if path.exists():
            self.wb = load_workbook(path)
        else:
            self.wb = Workbook()
            self.wb.remove(self.wb.active)

    def tab(self, title: str, *, user_columns: list[str] | None = None) -> XlsxGrid:
        ws = self.wb[title] if title in self.wb.sheetnames else self.wb.create_sheet(title)
        if title == "Jobs" and ws.freeze_panes is None:
            ws.freeze_panes = "B2"
        return XlsxGrid(ws, user_columns or [])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.stem}.saving.xlsx")
        self.wb.save(tmp)
        try:
            os.replace(tmp, self.path)
        except PermissionError as e:
            tmp.unlink(missing_ok=True)
            raise TrackerLocked(
                f"Skipped the tracker sync: {self.path.name} is locked by another program.",
                user_actions=[f"Close {self.path.name}, then run `jobhunt sync` again."],
            ) from e


# --- Google Sheets --------------------------------------------------------------------------


class SheetsGrid:
    def __init__(self, ws: Any) -> None:
        self.ws = ws

    def values(self) -> list[list[str]]:
        return [[cell_text(v) for v in row] for row in self.ws.get_all_values()]

    def _ensure_cols(self, n: int) -> None:
        if n > self.ws.col_count:
            self.ws.add_cols(n - self.ws.col_count)

    def set_cells(self, updates: list[tuple[int, int, str]]) -> None:
        if not updates:
            return
        from gspread.utils import rowcol_to_a1

        self._ensure_cols(max(c for _, c, _ in updates))
        body = [{"range": rowcol_to_a1(r, c), "values": [[v]]} for r, c, v in updates]
        for i in range(0, len(body), 500):
            self.ws.batch_update(body[i : i + 500], value_input_option="RAW")

    def append_rows(self, rows: list[list[str]]) -> None:
        if rows:
            self._ensure_cols(max(len(r) for r in rows))
            self.ws.append_rows(rows, value_input_option="RAW")

    def replace_all(self, rows: list[list[str]]) -> None:
        self.ws.clear()
        if rows:
            self._ensure_cols(max(len(r) for r in rows))
            self.ws.update(range_name="A1", values=rows, value_input_option="RAW")


class SheetsBackend:
    name = "sheets"

    def __init__(
        self, spreadsheet_id: str, service_account_file: str, *, client: Any = None
    ) -> None:
        self.location = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        if client is None:
            try:
                import gspread
            except ImportError as e:
                raise ConfigError(
                    "The Google Sheets tracker needs the [sheets] extra.",
                    next_actions=[
                        f"Reinstall with `{reinstall_command('sheets')}`, or set "
                        "tracker.kind: xlsx."
                    ],
                ) from e
            client = gspread.service_account(filename=str(Path(service_account_file).expanduser()))
        self.sheet = client.open_by_key(spreadsheet_id)

    def tab(self, title: str, *, user_columns: list[str] | None = None) -> SheetsGrid:
        try:
            ws = self.sheet.worksheet(title)
        except Exception as e:
            if type(e).__name__ != "WorksheetNotFound":
                raise
            ws = self.sheet.add_worksheet(title=title, rows=1000, cols=40)
        return SheetsGrid(ws)

    def save(self) -> None:  # Sheets writes are immediate
        return None
