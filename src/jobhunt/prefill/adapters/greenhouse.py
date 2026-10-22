"""Greenhouse (``job-boards.greenhouse.io/{board}/jobs/{id}``).

Verified against live forms (2026-10-18):
- Inputs carry the questions API field name as their id (``first_name``, ``question_123``).
  Question ids differ per job, so they come from that job's own form spec, never reused.
- Selects are react-select ARIA comboboxes: ``input#ID[role=combobox]`` with options
  ``#react-select-ID-option-N``; the chosen value shows in ``.select__single-value``.
- The location field (API name ``location``) is ``#candidate-location``, an autocomplete that
  only counts once an option is clicked.
- Phone: a country combobox ``#country`` inside ``.phone-input`` is set before ``#phone``; its
  list opens only once you type, and only after the page has hydrated.
- The resume goes through ``input#resume[type=file]``; the page uploads it to Greenhouse's S3
  bucket (allowed), and every POST to greenhouse.io is blocked during the fill.
- After the human submits, the URL gains ``application_id=``.
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

from jobhunt.bank.keys import country_names
from jobhunt.prefill.adapters.base import (
    Adapter,
    FieldMissing,
    FieldOutcome,
    NoOption,
    phone_country_option,
    value_text,
)
from jobhunt.prefill.guard import safe_click
from jobhunt.prefill.values import AnswerSheet, FillField

COMBO = frozenset({"select", "multiselect", "boolean", "radio", "checkbox"})


def _q(fid: str) -> str:
    return fid.replace("\\", "\\\\").replace('"', '\\"')


class Greenhouse(Adapter):
    ats: ClassVar[str] = "greenhouse"
    allow: ClassVar[tuple[str, ...]] = (
        r"^POST https://grnhse[a-z0-9-]*\.s3[a-z0-9.-]*\.amazonaws\.com/",  # resume upload
    )
    submit: ClassVar[tuple[str, ...]] = (
        r"^(POST|PUT|PATCH) https://([a-z0-9-]+\.)*greenhouse\.io/",
    )

    def ready(self, page: Any) -> None:
        self.first(page.locator("#application-form, form"), "the application form")
        self.settle(page)

    def confirmed(self, page: Any) -> bool:
        return "application_id=" in page.url or "/confirmation" in page.url

    def confirmation_id(self, page: Any) -> str | None:
        """Greenhouse adds ``application_id=`` to the confirmation URL."""
        found = parse_qs(urlsplit(page.url).query).get("application_id")
        return found[0] if found and found[0] else None

    # --- locating ---------------------------------------------------------------------------

    def _combo(self, page: Any, ff: FillField) -> Any:
        fid = "candidate-location" if ff.type == "location" else ff.id
        loc = page.locator(f'input[role=combobox][id="{_q(fid)}"]')
        if loc.count() == 0 and fid.startswith("demographic_"):
            # demographic question ids aren't in the DOM; their exact label is the only handle
            loc = page.get_by_role("combobox", name=ff.label, exact=True)
        return self.first(loc, f"#{fid}")

    def _input(self, page: Any, ff: FillField) -> Any:
        loc = page.locator(f'[id="{_q(ff.id)}"]:not([role=combobox])')
        return self.first(loc, f"#{ff.id}")

    def _file_box(self, page: Any, ff: FillField) -> Any:
        label = page.locator(f'[id="upload-label-{_q(ff.id)}"]')
        return self.first(label.locator("xpath=ancestor::*[contains(@class,'file-upload')][1]"),
                          f"upload box for {ff.id}")  # fmt: skip

    def anchor(self, page: Any, ff: FillField) -> Any | None:
        for sel in (f'[id="{_q(ff.id)}"]', f'[id="upload-label-{_q(ff.id)}"]'):
            loc = page.locator(sel)
            if loc.count():
                return loc.first
        if ff.type == "location":
            loc = page.locator("#candidate-location")
            return loc.first if loc.count() else None
        return None

    # --- filling ----------------------------------------------------------------------------

    def before_fill(self, page: Any, sheet: AnswerSheet) -> list[FieldOutcome]:
        phone = next((f for f in sheet.to_fill() if f.key == "identity.phone"), None)
        box = page.locator('.phone-input input[role=combobox][id="country"]')
        if phone is None or box.count() == 0:
            return []
        out = FieldOutcome(id="phone_country", label="Phone country", type="select",
                           required=False, status="filled")  # fmt: skip
        names = country_names(sheet.phone_country)
        try:
            safe_click(box.first)
            if names:  # live forms open the list only once you type
                box.first.press_sequentially(names[0])
            opts = page.locator('[id^="react-select-country-option-"]')
            opts.first.wait_for(state="visible", timeout=5_000)
            texts = [t.strip() for t in opts.all_inner_texts()]
            pick = phone_country_option(sheet.phone_country, texts)
            if pick is None:
                raise NoOption("set the phone country yourself")
            safe_click(opts.nth(texts.index(pick)))
            out.expected = out.actual = pick
        except Exception as e:
            self.escape(box.first)
            out.status, out.note = "no_option", f"phone country not set: {e}"
        return [out]

    def set_field(self, page: Any, ff: FillField, sheet: AnswerSheet) -> None:
        if ff.type == "file":
            self._upload(page, ff)
        elif ff.type == "location":
            box = self._combo(page, ff)
            safe_click(box)
            box.press_sequentially(str(ff.value).split(",")[0].strip())
            opts = page.locator('[id^="react-select-candidate-location-option-"]')
            try:
                self.choose(page, opts, str(ff.value), location=True)
            except NoOption:
                self.escape(box)
                raise
        elif ff.type in COMBO:
            box = self._combo(page, ff)
            values = ff.value if isinstance(ff.value, list) else [ff.value]
            opts = page.locator(
                f'[id^="react-select-{_q(box.get_attribute("id") or ff.id)}-option-"]'
            )
            for v in values:
                safe_click(box)
                try:
                    self.choose(page, opts, str(v))
                except NoOption:
                    self.escape(box)
                    raise
            self.escape(box)
        else:
            self._input(page, ff).fill(str(ff.value))

    def _upload(self, page: Any, ff: FillField) -> None:
        inp = page.locator(f'input[type=file][id="{_q(ff.id)}"]')
        if inp.count() == 0:
            raise FieldMissing(f"file input #{ff.id}")
        self.check_accept(inp.first, str(ff.value))
        inp.first.set_input_files(str(ff.value))
        name = value_text(ff)
        box = self._file_box(page, ff)
        box.get_by_text(name, exact=False).first.wait_for(timeout=self.parse_wait_s * 1000)

    # --- reading ----------------------------------------------------------------------------

    def read_field(self, page: Any, ff: FillField) -> str | None:
        if ff.type == "file":
            return self._file_box(page, ff).inner_text()
        if ff.type in COMBO or ff.type == "location":
            box = self._combo(page, ff)
            wrap = box.locator("xpath=ancestor::*[contains(@class,'select__control')][1]")
            multi = wrap.locator(".select__multi-value__label")
            if multi.count():
                return "; ".join(t.strip() for t in multi.all_inner_texts())
            single = wrap.locator(".select__single-value")
            return single.first.inner_text().strip() if single.count() else ""
        return self._input(page, ff).input_value()
