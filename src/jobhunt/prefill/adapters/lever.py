"""Lever (``jobs.lever.co/{site}/{id}/apply``).

Verified against live forms (2026-10-18):
- A server-rendered ``<form method=POST>``; fields are found by their ``name``. Submitting POSTs
  the form back to ``/apply``, which is blocked during the fill.
- Uploading the resume POSTs ``/parseResume`` (allowed), and Lever fills name, email, phone and
  company from it. So the resume goes first, then the engine waits for the parse and fills or
  overwrites the text fields.
- Location is an autocomplete (``GET /searchLocations``) with results in ``.dropdown-location``.
- Text, textarea and select fields are filled. Checkboxes, radios and the hCaptcha are left to the
  user, with the recommended value shown next to each.
- After the human submits, Lever shows its ``/thanks`` page.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from jobhunt.prefill.adapters.base import Adapter, FieldMissing, NoOption, pick_option
from jobhunt.prefill.values import AnswerSheet, FillField

_THANKS = re.compile(r"application (?:was )?submitted|thanks for applying", re.I)


def _q(name: str) -> str:
    return name.replace("\\", "\\\\").replace('"', '\\"')


class Lever(Adapter):
    ats: ClassVar[str] = "lever"
    allow: ClassVar[tuple[str, ...]] = (r"^POST https://jobs(\.eu)?\.lever\.co/parseResume(\?|$)",)
    submit: ClassVar[tuple[str, ...]] = (
        r"^(POST|PUT|PATCH) https://jobs(\.eu)?\.lever\.co/[^/?]+/[^/?]+/apply",
        r"^(POST|PUT|PATCH) https://api(\.eu)?\.lever\.co/",
    )
    upload_first: ClassVar[bool] = True

    def ready(self, page: Any) -> None:
        self.first(page.locator("#application-form, form"), "the application form")
        self.settle(page)

    def confirmed(self, page: Any) -> bool:
        if re.search(r"/thanks/?(\?|$)", page.url):
            return True
        try:
            return bool(_THANKS.search(page.locator("body").inner_text(timeout=1_000)))
        except Exception:
            return False

    def _el(self, page: Any, ff: FillField) -> Any:
        return self.first(page.locator(f'[name="{_q(ff.id)}"]'), f"[name={ff.id}]")

    def anchor(self, page: Any, ff: FillField) -> Any | None:
        loc = page.locator(f'[name="{_q(ff.id)}"]')
        return loc.first if loc.count() else None

    def set_field(self, page: Any, ff: FillField, sheet: AnswerSheet) -> None:
        if ff.type == "file":
            inp = page.locator(f'input[type=file][name="{_q(ff.id)}"]')
            if inp.count() == 0:
                raise FieldMissing(f"file input {ff.id}")
            self.check_accept(inp.first, str(ff.value))
            try:
                with page.expect_response(
                    lambda r: "/parseResume" in r.url, timeout=self.parse_wait_s * 1000
                ):
                    inp.first.set_input_files(str(ff.value))
            except Exception:  # no parse on this form, or it was slow: fill anyway
                pass
            page.wait_for_timeout(300)
        elif ff.type == "location":
            el = self._el(page, ff)
            el.fill("")
            el.press_sequentially(str(ff.value).split(",")[0].strip())
            self.choose(page, page.locator(".dropdown-location"), str(ff.value), location=True)
        elif ff.type in ("select", "multiselect"):
            el = self._el(page, ff)
            texts = [t.strip() for t in el.locator("option").all_inner_texts()]
            pick = pick_option(str(ff.value), texts)
            if pick is None:
                raise NoOption("no option matches your answer: pick it yourself")
            el.select_option(label=pick)
        elif ff.type in ("radio", "checkbox", "boolean"):
            raise NoOption("Lever: you tick this yourself")  # never reached: action is recommend
        else:
            self._el(page, ff).fill(str(ff.value))

    def read_field(self, page: Any, ff: FillField) -> str | None:
        if ff.type == "file":
            q = self._el(page, ff).locator(
                "xpath=ancestor::*[contains(@class,'application-question')][1]"
            )
            return q.first.inner_text()
        el = self._el(page, ff)
        if ff.type in ("select", "multiselect"):
            return el.evaluate("e => e.selectedIndex > 0 ? e.options[e.selectedIndex].text : ''")
        if ff.type in ("radio", "checkbox"):
            return "; ".join(
                page.locator(f'[name="{_q(ff.id)}"]:checked').evaluate_all(
                    "els => els.map(e => e.value)"
                )
            )
        return el.input_value()
