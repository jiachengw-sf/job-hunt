"""Ashby (``jobs.ashbyhq.com/{board}/{id}/application``).

Verified against live forms (2026-10-18):
- There is no ``<form>``; each field sits in ``[data-field-path=<field path>]``, and text inputs
  carry the path as id and name.
- Reads are GraphQL POSTs (``ApiJobPosting``, ``ApiOrganizationFromHostedJobsPageName``,
  ``ApiAutocompleteGeoLocation``). An upload runs ``ApiCreateFileUploadHandle`` → S3 →
  ``ApiAutofillApplicationFormWithUploadedResume``, and the autofill overwrites fields. So the
  resume goes **first**, the engine waits for the parse to finish, then fills or overwrites.
  Ashby also saves the draft as fields change (``ApiSetFormValue``, ``ApiSetFormValueToFile``
  attaches the upload). Those operations are allowed; anything else, and any operation named
  like a submit, is blocked during the fill.
- Yes/no questions are ``button[data-option=yes|no]`` toggles (``aria-pressed``). Single selects
  are radio groups and multi-selects checkbox groups, each option with its own ``<label>``.
- Location is a combobox in the ``_systemfield_location`` entry.
- Ashby can auto-reject on answers, so knockout questions are always highlighted for review.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from jobhunt.prefill.adapters.base import Adapter, FieldMissing, NoOption, norm, pick_option
from jobhunt.prefill.guard import safe_click
from jobhunt.prefill.values import AnswerSheet, FillField

_OPS = (
    "ApiJobPosting",
    "ApiOrganizationFromHostedJobsPageName",
    "ApiAutocompleteGeoLocation",
    "ApiCreateFileUploadHandle",
    "ApiAutofillApplicationFormWithUploadedResume",
    # Ashby saves the draft server-side as fields change; these are not submits
    "ApiSetFormValue",
    "ApiSetFormValueToFile",
)
_DONE = re.compile(
    r"application (?:was )?(?:successfully )?submitted|thank(?:s| you) for applying", re.I
)


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


class Ashby(Adapter):
    ats: ClassVar[str] = "ashby"
    allow: ClassVar[tuple[str, ...]] = (
        rf"^POST https://jobs\.ashbyhq\.com/api/non-user-graphql\?op=({'|'.join(_OPS)})$",
        r"^POST https://ashbyhq-[a-z0-9-]*uploaded-files[a-z0-9-]*\.s3[a-z0-9.-]*\.amazonaws\.com/",
    )
    submit: ClassVar[tuple[str, ...]] = (
        r"^(POST|PUT|PATCH) https://[^/]*ashbyhq\.com/.*submit",
        r"^(POST|PUT|PATCH) https://[^/]*ashbyhq\.com/api/(?!non-user-graphql\?)",
    )
    upload_first: ClassVar[bool] = True

    def ready(self, page: Any) -> None:
        self.first(page.locator("[data-field-path]"), "the application form")

    def confirmed(self, page: Any) -> bool:
        if page.locator(".ashby-application-form-success-container").count():
            return True
        try:
            return bool(_DONE.search(page.locator("body").inner_text(timeout=1_000)))
        except Exception:
            return False

    def _box(self, page: Any, ff: FillField) -> Any:
        return self.first(page.locator(f'[data-field-path="{_q(ff.id)}"]'), f"field {ff.id}")

    def anchor(self, page: Any, ff: FillField) -> Any | None:
        loc = page.locator(f'[data-field-path="{_q(ff.id)}"]')
        return loc.first if loc.count() else None

    def _option_labels(self, box: Any) -> tuple[Any, list[str]]:
        labels = box.locator("input[type=radio] ~ label, input[type=checkbox] ~ label, "
                             "span:has(> input[type=radio]) + label, "
                             "span:has(> input[type=checkbox]) + label")  # fmt: skip
        return labels, [t.strip() for t in labels.all_inner_texts()]

    def set_field(self, page: Any, ff: FillField, sheet: AnswerSheet) -> None:
        box = self._box(page, ff)
        if ff.type == "file":
            inp = box.locator("input[type=file]")
            if inp.count() == 0:
                raise FieldMissing(f"file input in {ff.id}")
            self.check_accept(inp.first, str(ff.value))
            try:
                with page.expect_response(
                    lambda r: "op=ApiAutofillApplicationFormWithUploadedResume" in r.url,
                    timeout=self.parse_wait_s * 1000,
                ):
                    inp.first.set_input_files(str(ff.value))
            except Exception:  # no autofill on this form, or it was slow: fill anyway
                pass
            page.wait_for_timeout(500)  # let the autofill land before overwriting it
        elif ff.type == "boolean":
            want = "yes" if norm(str(ff.value)) == "yes" else "no"
            btn = box.locator(f'button[data-option="{want}"]')
            if btn.count() == 0:
                raise FieldMissing(f"yes/no buttons in {ff.id}")
            if btn.first.get_attribute("aria-pressed") != "true":
                safe_click(btn.first)
        elif ff.type == "location":
            inp = self.first(box.locator("input[role=combobox], input"), f"location in {ff.id}")
            inp.fill("")
            inp.press_sequentially(str(ff.value).split(",")[0].strip())
            opts = page.locator("[role=listbox] [role=option], [role=option]")
            try:
                self.choose(page, opts, str(ff.value), location=True)
            except NoOption:
                self.escape(inp)
                raise
        elif ff.type in ("select", "radio", "multiselect", "checkbox"):
            labels, texts = self._option_labels(box)
            if not texts:
                raise FieldMissing(f"options in {ff.id}")
            values = ff.value if isinstance(ff.value, list) else [ff.value]
            for v in values:
                pick = pick_option(str(v), texts)
                if pick is None:
                    raise NoOption("no option matches your answer: pick it yourself")
                label = labels.nth(texts.index(pick))
                if not label.evaluate("l => !!(l.control && l.control.checked)"):
                    safe_click(label)
        else:
            loc = box.locator(f'[id="{_q(ff.id)}"], [name="{_q(ff.id)}"], input, textarea')
            self.first(loc, f"input {ff.id}").fill(str(ff.value))

    def read_field(self, page: Any, ff: FillField) -> str | None:
        box = self._box(page, ff)
        if ff.type == "file":
            return box.inner_text()
        if ff.type == "boolean":
            on = box.locator('button[data-option][aria-pressed="true"]')
            return {"yes": "Yes", "no": "No"}.get(on.first.get_attribute("data-option") or "", "") \
                if on.count() else ""  # fmt: skip
        if ff.type in ("select", "radio", "multiselect", "checkbox"):
            labels, texts = self._option_labels(box)
            checked = labels.evaluate_all("ls => ls.map(l => !!(l.control && l.control.checked))")
            return "; ".join(t for t, c in zip(texts, checked, strict=False) if c)
        if ff.type == "location":
            return box.locator("input[role=combobox], input").first.input_value()
        loc = box.locator(f'[id="{_q(ff.id)}"], [name="{_q(ff.id)}"], input, textarea')
        return loc.first.input_value()
