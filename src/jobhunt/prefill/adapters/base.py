"""What every ATS adapter shares: fill order, read-back, outcomes, location matching.

An adapter knows one ATS's page: how to find a field by its form-spec id, how to set each kind
of field, how to read it back, which non-GET requests a fill needs (``allow``), which endpoints
submit (``submit``), and what the page looks like after the human submits. Adapters click only
through :func:`jobhunt.prefill.guard.safe_click`. A field that can't be found or set is reported
and highlighted, never guessed.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from jobhunt.bank.keys import country_names
from jobhunt.prefill.guard import RouteGuard, SubmitRefused, safe_click, safe_press
from jobhunt.prefill.values import AnswerSheet, FillField

log = logging.getLogger(__name__)

Status = Literal[
    "filled",  # set and read back equal
    "mismatch",  # set, but the page shows something else
    "not_found",  # the field isn't on the page (selector drift): highlighted, logged
    "no_option",  # no option on the page matches confidently: left for the user
    "refused",  # the click guard refused the control
    "file_rejected",  # the form doesn't take this file type
    "error",  # anything else went wrong setting it
    "recommend",  # left for the user with the recommended value shown
    "left",  # left for the user on purpose (human-only, essay, unanswered)
    "skipped",  # optional and unanswered
    "autofilled",  # empty in answers.json but the ATS filled it (resume parse): check it
    "unlisted",  # the page requires it but the form spec doesn't list it: never left silently
]
NEEDS_YOU = frozenset(
    {"mismatch", "not_found", "no_option", "refused", "file_rejected", "error", "recommend",
     "left", "autofilled", "unlisted"}
)  # fmt: skip
TIMEOUT_MS = 5_000


class FieldOutcome(BaseModel):
    id: str
    label: str
    type: str
    required: bool
    status: Status
    note: str | None = None
    human: list[str] = Field(default_factory=list)
    # local only (highlight notes, the review page); stripped from every envelope
    expected: str | None = Field(None, exclude=True)
    actual: str | None = Field(None, exclude=True)

    @property
    def needs_you(self) -> bool:
        return self.status in NEEDS_YOU or bool(self.human)


class FieldMissing(Exception):
    pass


class NoOption(Exception):
    pass


class FileRejected(Exception):
    pass


US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
    "PR": "Puerto Rico",
}  # fmt: skip


def norm(s: str | None) -> str:
    return " ".join((s or "").replace(" ", " ").split()).casefold()


def digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def pick_location(value: str, options: list[str]) -> str | None:
    """The one option naming the same city and region as ``value`` ("City, ST"), else None."""
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        return None
    city, region = norm(parts[0]), (parts[1] if len(parts) > 1 else None)
    regions = set()
    if region:
        regions = {norm(region), norm(US_STATE_NAMES.get(region.upper(), region))}
    hits = []
    for o in options:
        oparts = [norm(p) for p in o.split(",")]
        if not oparts or oparts[0] != city:
            continue
        if regions and not regions & set(oparts[1:]):
            continue
        hits.append(o)
    distinct = {norm(h) for h in hits}
    return hits[0] if len(distinct) == 1 else None


def pick_option(value: str, options: list[str]) -> str | None:
    """The option whose text equals ``value`` (whitespace and case aside)."""
    want = norm(value)
    hits = [o for o in options if norm(o) == want]
    return hits[0] if hits else None


def phone_country_option(code: str | None, options: list[str]) -> str | None:
    """The phone-country option for an ISO code ("Canada +1", "United States +1")."""
    if not code:
        return None
    names = [norm(n) for n in country_names(code) if len(n) > 2]
    for o in options:
        head = norm(re.sub(r"\+\d[\d\s-]*$", "", o))
        if head in names:
            return o
    return None


def value_text(ff: FillField) -> str:
    v = ff.value
    if isinstance(v, list):
        return "; ".join(v)
    if ff.type == "file" and v:
        return Path(v).name
    return v or ""


# Every required, visible control on the page: what the form spec must cover.
_REQUIRED_JS = """() => [...document.querySelectorAll('input, textarea, select')].filter(el =>
  (el.required || el.getAttribute('aria-required') === 'true') && el.type !== 'hidden'
  && el.getAttribute('aria-hidden') !== 'true' && !el.closest('[hidden], template')
).map(el => {
  const by = el.getAttribute('aria-labelledby');
  const lab = (el.labels && el.labels[0]) || (by && document.getElementById(by));
  return {id: el.id || '', name: el.getAttribute('name') || '',
          label: ((lab && lab.innerText) || el.getAttribute('aria-label') || '').trim()};
})"""

# Bot-check and CAPTCHA widgets load as they normally would, so the human's own submit later
# isn't broken by a widget the fill starved. jobhunt never solves or touches them.
WIDGETS = (
    r"^POST https://(api2?\.)?hcaptcha\.com/checksiteconfig\?",
    r"^POST https://www\.(google|recaptcha)\.(com|net)/recaptcha/(enterprise|api2)/(reload|clr)\?",
    r"^POST https://[^/]+/cdn-cgi/challenge-platform/",
)


class Adapter:
    """Base adapter. Subclasses set the class attributes and the per-type hooks."""

    ats: ClassVar[str] = ""
    allow: ClassVar[tuple[str, ...]] = ()
    submit: ClassVar[tuple[str, ...]] = ()
    upload_first: ClassVar[bool] = False  # the ATS parses the resume and overwrites fields

    def __init__(self, parse_wait_s: float = 20.0) -> None:
        self.parse_wait_s = parse_wait_s

    def guard(self) -> RouteGuard:
        return RouteGuard.of(allow=(*self.allow, *WIDGETS), submit=self.submit)

    # --- page hooks (subclasses) ------------------------------------------------------------

    def ready(self, page: Any) -> None:
        """Wait until the form is on the page."""

    def confirmed(self, page: Any) -> bool:
        """True once the page shows the ATS's confirmation after the human submits."""
        raise NotImplementedError

    def confirmation_id(self, page: Any) -> str | None:
        """The ATS's own id for the submitted application, when the confirmation shows one."""
        return None

    def set_field(self, page: Any, ff: FillField, sheet: AnswerSheet) -> None:
        raise NotImplementedError

    def read_field(self, page: Any, ff: FillField) -> str | None:
        raise NotImplementedError

    def anchor(self, page: Any, ff: FillField) -> Any | None:
        """The element to outline when highlighting this field (None if not on the page)."""
        raise NotImplementedError

    def before_fill(self, page: Any, sheet: AnswerSheet) -> list[FieldOutcome]:
        """Anything that must happen before the fields (e.g. phone country)."""
        return []

    def page_ids(self, ff: FillField) -> set[str]:
        """The ids or names a field's control can carry on the page (default: its own id)."""
        return {ff.id}

    # --- shared ----------------------------------------------------------------------------

    def matches(self, ff: FillField, actual: str | None) -> bool:
        if actual is None:
            return False
        if ff.type == "phone" or (ff.key or "") == "identity.phone":
            want = digits(value_text(ff))
            got = digits(actual)
            return bool(want) and (got.endswith(want) or (want.endswith(got) and len(got) >= 7))
        if ff.type == "file":
            return norm(value_text(ff)) in norm(actual)
        if ff.type == "location":
            return bool(actual.strip())  # the chosen option, checked when picked
        if isinstance(ff.value, list):
            return {norm(v) for v in ff.value} == {norm(x) for x in actual.split(";") if x.strip()}
        return norm(value_text(ff)) == norm(actual)

    def order(self, sheet: AnswerSheet) -> list[FillField]:
        fields = sheet.to_fill()
        if self.upload_first:
            fields.sort(key=lambda f: f.type != "file")
        return fields

    def fill(self, page: Any, sheet: AnswerSheet) -> list[FieldOutcome]:
        """Fill every ``fill`` field, then read every field back and compare."""
        self.ready(page)
        before = (
            {f.id: self._safe_read(page, f) for f in sheet.fields if f.action == "skip"}
            if self.upload_first
            else {}
        )
        outcomes: dict[str, FieldOutcome] = {o.id: o for o in self.before_fill(page, sheet)}
        for ff in self.order(sheet):
            status, note = "filled", None
            try:
                self.set_field(page, ff, sheet)
            except SubmitRefused as e:
                status, note = "refused", f"the click guard refused a control here ({e})"
            except FieldMissing as e:
                status, note = "not_found", f"couldn't find this field on the page ({e})"
            except NoOption as e:
                status, note = "no_option", str(e)
            except FileRejected as e:
                status, note = "file_rejected", str(e)
            except Exception as e:  # selector drift, timeouts: report, never guess
                log.warning("%s: filling %s failed: %s", self.ats, ff.id, e)
                status, note = "error", f"couldn't set this field: {type(e).__name__}"
            outcomes[ff.id] = self._outcome(ff, status, note)
        for ff in sheet.fields:  # read back everything after all writes
            o = outcomes.get(ff.id)
            if ff.action == "fill" and o is not None and o.status == "filled":
                actual = self._safe_read(page, ff)
                o.actual = actual
                if not self.matches(ff, actual):
                    o.status = "mismatch"
                    o.note = "the page shows a different value than answers.json"
                    log.warning("%s: read-back mismatch on %s", self.ats, ff.id)
            elif ff.action != "fill" and ff.id not in outcomes:
                outcomes[ff.id] = self._unfilled(page, ff, before.get(ff.id))
        for o in self.unlisted(page, sheet):
            outcomes.setdefault(o.id, o)
        return [outcomes[f.id] for f in sheet.fields if f.id in outcomes] + [
            o for k, o in outcomes.items() if k not in {f.id for f in sheet.fields}
        ]

    def unlisted(self, page: Any, sheet: AnswerSheet) -> list[FieldOutcome]:
        """Required controls on the page that no sheet field covers. A form section the reader
        doesn't know about is reported for the user, never left blank in silence."""
        known: set[str] = set()
        for ff in sheet.fields:
            known |= self.page_ids(ff)
        try:
            controls = page.evaluate(_REQUIRED_JS)
        except Exception:
            return []
        out: list[FieldOutcome] = []
        for c in controls:
            ref = c.get("id") or c.get("name") or ""
            if not ref or known & {c.get("id"), c.get("name")} or ref in {o.id for o in out}:
                continue
            out.append(
                FieldOutcome(
                    id=ref,
                    label=c.get("label") or ref,
                    type="unknown",
                    required=True,
                    status="unlisted",
                    note="the page requires this field but jobhunt didn't read it from the form",
                )
            )
        return out

    def _outcome(self, ff: FillField, status: str, note: str | None) -> FieldOutcome:
        return FieldOutcome(
            id=ff.id,
            label=ff.label,
            type=ff.type,
            required=ff.required,
            status=status,  # type: ignore[arg-type]
            note=note or ff.note,
            human=ff.human,
            expected=value_text(ff) or None,
        )

    def _unfilled(self, page: Any, ff: FillField, before: str | None) -> FieldOutcome:
        if ff.action == "recommend":
            return self._outcome(ff, "recommend", ff.note)
        if ff.action == "human":
            return self._outcome(ff, "left", ff.note)
        now = self._safe_read(page, ff) if self.upload_first else None
        if now and now != before:
            o = self._outcome(ff, "autofilled", "the ATS filled this from your resume: check it")
            o.actual = now
            return o
        return self._outcome(ff, "skipped", None)

    def _safe_read(self, page: Any, ff: FillField) -> str | None:
        try:
            return self.read_field(page, ff)
        except Exception:
            return None

    # --- helpers for subclasses --------------------------------------------------------------

    @staticmethod
    def settle(page: Any, timeout_ms: int = 10_000) -> None:
        """Let the page's scripts hydrate: server-rendered widgets ignore input until then."""
        with contextlib.suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=timeout_ms)

    @staticmethod
    def first(locator: Any, what: str) -> Any:
        try:
            locator.first.wait_for(state="attached", timeout=TIMEOUT_MS)
        except Exception as e:
            raise FieldMissing(what) from e
        return locator.first

    @staticmethod
    def check_accept(file_input: Any, path: str) -> None:
        accept = (file_input.get_attribute("accept") or "").lower()
        ext = Path(path).suffix.lower()
        if accept and ext and ext not in {a.strip() for a in accept.split(",")}:
            raise FileRejected(
                f"this form takes {accept.replace(',', ', ')}; your resume is {ext}. Add a PDF "
                "with `jobhunt resume add`."
            )

    @staticmethod
    def choose(
        page: Any,
        options: Any,
        wanted: str,
        *,
        location: bool = False,
        picker: Callable[[str, list[str]], str | None] | None = None,
        searched: bool = False,
    ) -> str:
        """Click the option matching ``wanted`` among visible ``options``. ``picker`` replaces
        exact matching; ``searched`` waits for results typed into a search box to arrive."""
        try:
            options.first.wait_for(state="visible", timeout=TIMEOUT_MS)
        except Exception as e:
            raise NoOption("no options appeared") from e
        pick_fn = picker or (pick_location if location else pick_option)
        deadline = time.monotonic() + (TIMEOUT_MS / 1000 if searched else 0)
        while True:
            texts = [t.strip() for t in options.all_inner_texts()]
            pick = pick_fn(wanted, texts)
            if pick is not None or time.monotonic() >= deadline:
                break
            page.wait_for_timeout(250)
        if pick is None:
            raise NoOption(
                "no option on the page matches your answer confidently: pick it yourself"
            )
        safe_click(options.nth(texts.index(pick)))
        return pick

    @staticmethod
    def escape(locator: Any) -> None:
        with contextlib.suppress(Exception):
            safe_press(locator, "Escape")
