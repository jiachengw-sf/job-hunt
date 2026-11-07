# ruff: noqa: E501  (page scripts and step tables read better unwrapped)
"""Workday: a multi-step application behind each employer's own sign-in.

Unlike the other ATSs, a Workday form can't be read before the browser opens: the human signs in
(jobhunt never does), and each step (My Information, My Experience, Application Questions,
Voluntary Disclosures, ...) appears only after the previous one is saved. So this adapter also
*reads*: :meth:`Workday.read_step` turns the step on screen into a :class:`FormSpec`, which goes
through the same classify -> build_sheet -> validate path as every other form before anything is
typed (see :mod:`jobhunt.prefill.workday_flow`).

Page facts this relies on (both newer and older Workday UIs):

- every question sits in a ``[data-automation-id="formField-<name>"]`` box; ``<name>`` is the
  field's id (stable per step; questionnaire boxes use Workday's own hashes);
- dropdowns are ``button[aria-haspopup=listbox]`` with ``[role=option]`` items, loaded late;
- search prompts (source, school, field of study, skills, phone code) are an input in a box with
  a prompt icon; options are ``[role=option]`` in ``[data-automation-id=activeListContainer]``,
  some are categories ("Campus" -> "Campus: Career Fair"), some lists open only via "All", and
  some search only on Enter (:func:`jobhunt.prefill.guard.search_enter`);
- dates are MM / YYYY (or YYYY, or MM/DD/YYYY) spinbutton sections that ignore clicks;
- repeatable sections ("Work Experience 1", "Education 1", ...) have controls with ids like
  ``workExperience-16--jobTitle`` and an "Add" / "Add Another" button;
- Workday saves every field as it changes (POSTs under ``/wday/calypso/cxs/``) and each step on
  Save and Continue (PUT/POST under ``/wday/cxs/<tenant>/jobapplication/<id>/<section>``). Those
  are allowed; anything that names ``submit`` is always blocked.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobhunt.bank.keys import BUILTIN
from jobhunt.forms.model import FormField, FormSpec
from jobhunt.prefill.adapters.base import (
    Adapter,
    FieldMissing,
    FieldOutcome,
    NoOption,
    norm,
    pick_option,
    value_text,
)
from jobhunt.prefill.guard import SubmitRefused, safe_click, safe_press, search_enter
from jobhunt.prefill.values import AnswerSheet, FillField

log = logging.getLogger(__name__)

HOST = r"https://[^/]+\.(myworkdayjobs|myworkdaysite|workday)\.com"
# Repeatable sections filled from the profile, not from the answer sheet.
REPEATS = ("workExperience", "education", "language", "webAddress", "certification")
_REPEAT_ID = re.compile(rf"^({'|'.join(REPEATS)})-\d+--")
PLACEHOLDER = re.compile(r"^\s*(select one|select\.\.\.|no items\.?)\s*$", re.I)
OPTION_WAIT_S = 8.0
_SEP = re.compile(r"\s*(?::|>|→)\s*")

STEP_JS = """() => {
  const a = document.querySelector('[data-automation-id=progressBarActiveStep]');
  if (!a) return null;
  return (a.innerText || '').replace(/\\s+/g, ' ').replace(/^current step \\d+ of \\d+\\s*/i, '').trim();
}"""

# The step's top-level questions (not inside a repeatable section), tagged data-jh-box=<i>.
BOXES_JS = r"""(repeat) => {
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  const txt = e => (e ? (e.innerText || e.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const rep = new RegExp(repeat);
  document.querySelectorAll('[data-jh-box]').forEach(e => e.removeAttribute('data-jh-box'));
  const out = [];
  for (const b of document.querySelectorAll('[data-automation-id^=formField-]')) {
    if (!vis(b) || b.parentElement.closest('[data-automation-id^=formField-]')) continue;
    const ctl = b.querySelector('input:not([type=hidden]), textarea, button[aria-haspopup=listbox]');
    if (ctl && rep.test(ctl.id || '')) continue;
    const name = b.getAttribute('data-automation-id').slice('formField-'.length);
    const lab = b.querySelector('label, legend');
    let label = txt(lab);
    if (label.replace(/\*/g, '').trim().length < 3) {
      const rt = b.querySelector('[data-automation-id=richText]');
      label = txt(rt) || label;
    }
    const file = b.querySelector('input[type=file]');
    const kind = file ? 'file'
      : b.querySelector('button[aria-haspopup=listbox]') ? 'listbox'
      : b.querySelector('input[type=radio]') ? 'radio'
      : b.querySelector('[data-automation-id=promptIcon],[data-automation-id=selectedItemList],[data-automation-id=searchBox],[data-automation-id=multiselectInputContainer]') ? 'prompt'
      : b.querySelector('[id$=dateSectionYear-input]') ? 'date'
      : b.querySelector('input[type=checkbox]') ? 'checkbox'
      : b.querySelector('textarea') ? 'textarea'
      : b.querySelector('input') ? 'text' : 'other';
    const radios = [...b.querySelectorAll('input[type=radio]')].map(r => {
      const l = r.id && b.querySelector(`label[for="${r.id}"]`); return txt(l) || r.value; });
    const checks = [...b.querySelectorAll('input[type=checkbox]')];
    const btn = b.querySelector('button[aria-haspopup=listbox]');
    const inp = b.querySelector('input:not([type=radio]):not([type=checkbox]):not([type=file]), textarea');
    const pills = [...b.querySelectorAll('[data-automation-id=selectedItem]')].map(txt);
    let has = false;
    if (kind === 'listbox') has = !!txt(btn) && !/^select one$/i.test(txt(btn));
    else if (kind === 'radio') has = [...b.querySelectorAll('input[type=radio]')].some(r => r.checked);
    else if (kind === 'checkbox') has = checks.some(c => c.checked);
    else if (kind === 'prompt') has = pills.length > 0;
    else if (kind === 'date') has = !!(b.querySelector('[id$=dateSectionYear-input]').value);
    else if (kind === 'file') has = !!b.querySelector('[data-automation-id=file-upload-successful], [data-automation-id=fileName]');
    else if (inp) has = !!inp.value;
    const max = inp && inp.maxLength > 0 ? inp.maxLength : null;
    const i = out.length; b.setAttribute('data-jh-box', String(i));
    out.push({i, name, kind, label: label.slice(0, 500), required: /\*\s*$/.test(txt(lab)) ||
      !!b.querySelector('[aria-required=true],[required]'), radios, n_checks: checks.length,
      control: ctl ? (ctl.id || '') : '', has, max});
  }
  return out;
}"""

# Visible options of the open popup, tagged data-jh-opt=<i>; selected pills in fields are skipped.
OPTS_JS = r"""() => {
  document.querySelectorAll('[data-jh-opt]').forEach(e => e.removeAttribute('data-jh-opt'));
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  const out = [];
  for (const e of document.querySelectorAll('[role=option]')) {
    if (!vis(e) || e.closest('[data-automation-id=selectedItemList]')) continue;
    if (e.closest('[data-automation-id^=formField-]') && !e.closest('[role=listbox],[data-automation-id=activeListContainer]')) continue;
    e.setAttribute('data-jh-opt', String(out.length));
    out.push((e.innerText || '').replace(/\s+/g, ' ').trim());
  }
  return out;
}"""

SCROLL_JS = r"""() => {
  const c = [...document.querySelectorAll('[data-automation-id=activeListContainer],[role=listbox]')]
    .find(e => e.getClientRects().length);
  let s = c;
  while (s && !(s.clientHeight > 40 && s.scrollHeight > s.clientHeight + 2
         && /auto|scroll/.test(getComputedStyle(s).overflowY))) s = s.parentElement;
  if (!s) return false;
  const before = s.scrollTop; s.scrollTop = before + s.clientHeight * 0.8;
  return s.scrollTop > before;
}"""

ENTRIES_JS = r"""(kind) => {
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  const out = {};
  for (const e of document.querySelectorAll(`[id^="${kind}-"]`)) {
    const m = e.id.match(new RegExp(`^(${kind}-\\d+)--(.+)$`));
    if (!m || !vis(e)) continue;
    const box = e.closest('[data-automation-id^=formField-]');
    const lab = box && box.querySelector('label, legend');
    (out[m[1]] = out[m[1]] || []).push({id: e.id, key: m[2], tag: e.tagName.toLowerCase(),
      popup: e.getAttribute('aria-haspopup') || '',
      prompt: !!(box && box.querySelector('[data-automation-id=promptIcon],[data-automation-id=selectedItemList],[data-automation-id=searchBox]')),
      label: lab ? (lab.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80) : ''});
  }
  return out;
}"""

ADD_JS = r"""(title) => {
  document.querySelectorAll('[data-jh-add]').forEach(e => e.removeAttribute('data-jh-add'));
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  for (const h of document.querySelectorAll('h1,h2,h3,h4,legend,[role=heading]')) {
    if ((h.innerText || '').trim() !== title || !vis(h)) continue;
    let sec = h.parentElement;
    for (let i = 0; i < 6 && sec; i++, sec = sec.parentElement) {
      const btns = [...sec.querySelectorAll('[data-automation-id=add-button]')].filter(vis);
      if (btns.length) { btns[btns.length - 1].setAttribute('data-jh-add', '1'); return true; }
    }
  }
  return false;
}"""

CHECK_JS = r"""() => {
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  const txt = e => (e ? (e.innerText || '') : '').replace(/\s+/g, ' ').trim();
  const empty = [];
  for (const b of document.querySelectorAll('[data-automation-id^=formField-]')) {
    if (!vis(b)) continue;
    const lab = b.querySelector('label, legend');
    if (!((lab && /\*/.test(txt(lab))) || b.querySelector('[aria-required=true],[required]'))) continue;
    const radios = b.querySelectorAll('input[type=radio]');
    const checks = b.querySelectorAll('input[type=checkbox]');
    const pills = b.querySelectorAll('[data-automation-id=selectedItem]');
    const btn = b.querySelector('button[aria-haspopup=listbox]');
    const file = b.querySelector('input[type=file]');
    const inp = b.querySelector('input:not([type=radio]):not([type=checkbox]):not([type=file]), textarea');
    let ok = true;
    if (file) ok = !!b.querySelector('[data-automation-id=file-upload-successful], [data-automation-id=fileName]');
    else if (radios.length) ok = [...radios].some(r => r.checked);
    else if (pills.length) ok = true;
    else if (btn) ok = !!txt(btn) && !/^select one$/i.test(txt(btn));
    else if (checks.length) ok = [...checks].some(r => r.checked);
    else if (inp) ok = !!inp.value;
    if (!ok) empty.push(txt(lab).replace(/\*/g, '').trim().slice(0, 100));
  }
  const errors = [...document.querySelectorAll('[data-automation-id=errorMessage],[data-automation-id=errorBanner],[aria-invalid=true]')]
    .filter(vis).map(e => (txt(e) || e.id || 'invalid field').slice(0, 160));
  return {empty, errors};
}"""

# Workday's own field names are the same on every employer: these map to the bank's canonical
# questions, so an address part never gets the "City, ST" answer and the previous-employer radio
# reaches ``experience.previously_employed`` (the employer's name is "company" when matching).
CANONICAL = {
    "legalName--firstName": "First name",
    "legalName--lastName": "Last name",
    "addressLine1": "Street address",
    "city": "Address city",
    "countryRegion": "Address state",
    "postalCode": "Postal code",
    "phoneNumber": "Phone number",
    "email": "Email",
    "candidateIsPreviousWorker": "Have you been previously employed by company",
}

CONFIRM = re.compile(
    r"application (has been |was )?(successfully )?submitted|thank you for (applying|your application)"
    r"|successfully applied",
    re.I,
)


@dataclass
class Box:
    """One top-level question on the step, as read from the page."""

    i: int
    name: str
    kind: str
    label: str
    required: bool
    control: str = ""
    has: bool = False
    radios: list[str] = field(default_factory=list)
    n_checks: int = 0
    max: int | None = None


@dataclass
class StepRead:
    name: str
    spec: FormSpec
    boxes: dict[str, Box]


def clean_label(s: str) -> str:
    return re.sub(r"\s*\*\s*$", "", " ".join((s or "").split())).strip()


def parse_ym(v: str | None) -> tuple[int | None, int | None, int | None]:
    """(month, day, year) from "2030-05", "2030-05-24", "05/2030", "May 2030" or "2030"."""
    if not v:
        return None, None, None
    v = str(v)
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", v.strip()):
        return int(m[2]), int(m[3]) if m[3] else None, int(m[1])
    if m := re.fullmatch(r"(\d{1,2})/(?:(\d{1,2})/)?(\d{4})", v.strip()):
        return int(m[1]), int(m[2]) if m[2] else None, int(m[3])
    months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    if m := re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{4})", v.strip()):
        mon = m[1].lower()
        if mon in months:
            return months.index(mon) + 1, None, int(m[2])
    if m := re.fullmatch(r"(\d{4})", v.strip()):
        return None, None, int(m[1])
    return None, None, None


class Workday(Adapter):
    ats = "workday"
    allow = (
        # per-field autosave and step bookkeeping
        rf"^(POST|PUT) {HOST}/wday/calypso/cxs/jobapplication/[^/]+/(jobapplication|package)/",
        # read-only: the step's questionnaire definition
        rf"^POST {HOST}/wday/calypso/cxs/common/[^/]+/questionnaire/[^/?]+/definition",
        # step saves on Save and Continue (never anything named submit; see ``submit``)
        rf"^(PUT|POST) {HOST}/wday/cxs/[^/]+/jobapplication/[0-9a-f]+/(?!submit)[A-Za-z]+(\?|$)",
        # resume upload
        rf"^POST {HOST}/wday/.*(upload|attachment)",
    )
    submit = (r"/submit", r"submitApplication", r"/review/submit")

    def __init__(self, parse_wait_s: float = 20.0) -> None:
        super().__init__(parse_wait_s)
        self.boxes: dict[str, Box] = {}

    # --- small page helpers ----------------------------------------------------------------

    @staticmethod
    def center(loc: Any) -> None:
        """Workday's sticky footer covers the bottom of the viewport: scroll targets to the
        middle before acting on them."""
        with contextlib.suppress(Exception):
            loc.evaluate("e => e.scrollIntoView({block: 'center', inline: 'nearest'})")

    def safe_click(self, loc: Any) -> None:
        self.center(loc)
        safe_click(loc)

    def box(self, page: Any, fid: str) -> Any:
        """A question's box by Workday's own field name (stable across re-renders; the page
        redraws parts of itself as fields are saved)."""
        if fid == "resume":
            loc = page.locator("[data-automation-id^=formField-]:has(input[type=file])")
        else:
            loc = page.locator(f"[data-automation-id='formField-{fid}']")
        if not loc.count():
            raise FieldMissing(fid)
        return loc.first

    @staticmethod
    def options(page: Any) -> list[str]:
        return page.evaluate(OPTS_JS)

    @staticmethod
    def pills(box: Any) -> list[str]:
        loc = box.locator("[data-automation-id=selectedItem]")
        return [t.strip() for t in loc.all_inner_texts()] if loc.count() else []

    def wait_options(self, page: Any, seconds: float = OPTION_WAIT_S) -> list[str]:
        end = time.monotonic() + seconds
        texts: list[str] = []
        while time.monotonic() < end:
            page.wait_for_timeout(250)
            texts = self.options(page)
            if [t for t in texts if not PLACEHOLDER.match(t)]:
                break
        return texts

    # --- steps -----------------------------------------------------------------------------

    @staticmethod
    def step_name(page: Any) -> str | None:
        try:
            return page.evaluate(STEP_JS)
        except Exception:
            return None

    @staticmethod
    def signing_in(page: Any) -> bool:
        sel = (
            "[data-automation-id=signInContent],[data-automation-id=signInForm],"
            "[data-automation-id=createAccountForm],[data-automation-id=applyAdventurePage]"
        )
        return page.locator(sel).count() > 0

    def on_form(self, page: Any) -> bool:
        name = self.step_name(page)
        return (
            bool(name)
            and not self.signing_in(page)
            and not re.search(r"sign in|create account", name or "", re.I)
            and page.locator("[data-automation-id^=formField-]").count() > 0
        )

    @staticmethod
    def on_review(page: Any) -> bool:
        return bool(re.search(r"^review$", Workday.step_name(page) or "", re.I))

    @staticmethod
    def step_check(page: Any) -> dict[str, list[str]]:
        return page.evaluate(CHECK_JS)

    def save_and_continue(self, page: Any, timeout_s: float = 25.0) -> str | None:
        """Click Save and Continue (never Submit) and wait for the next step's name."""
        before = self.step_name(page)
        nxt = page.locator("[data-automation-id=pageFooterNextButton]")
        if not nxt.count():
            raise FieldMissing("Save and Continue")
        label = (nxt.first.inner_text() or "").strip()
        if not re.fullmatch(r"(save and )?continue|next", label, re.I):
            raise SubmitRefused(f"the next button says “{label}”")
        self.safe_click(nxt.first)
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            page.wait_for_timeout(400)
            now = self.step_name(page)
            if now and now != before:
                return now
        return None

    def confirmed(self, page: Any) -> bool:
        if self.step_name(page):
            return False  # still inside the application flow
        try:
            text = page.locator("body").inner_text(timeout=2_000)
        except Exception:
            return False
        return bool(CONFIRM.search(text))

    # --- reading a step into a FormSpec -----------------------------------------------------

    def read_step(self, page: Any, url: str | None = None) -> StepRead:
        """The step on screen as a FormSpec (top-level questions only). Dropdowns are opened to
        read their options and closed again; nothing is chosen."""
        name = self.step_name(page) or ""
        raw = page.evaluate(BOXES_JS, f"^({'|'.join(REPEATS)})-\\d+--")
        self.boxes = {}
        fields: list[FormField] = []
        eeo = bool(re.search(r"voluntary|self.?identif|disclosure|disabilit", name, re.I))
        for r in raw:
            b = Box(**r)
            if not b.name and b.kind == "file":
                b.name = "resume"  # Workday's resume box is "formField-" with no name
            if not b.name or b.name in self.boxes or b.kind == "other":
                continue
            if b.name in ("countryPhoneCode", "phoneType", "preferredCheck", "extension",
                          "addressLine2", "regionSubdivision1") and b.kind != "file":  # fmt: skip
                # set from the bank's phone country, or optional page furniture
                self.boxes[b.name] = b
                continue
            self.boxes[b.name] = b
            label = CANONICAL.get(b.name) or clean_label(b.label)
            opts: list[str] = []
            ftype = "text"
            if b.kind == "listbox":
                ftype, opts = "select", self._read_listbox(page, b)
            elif b.kind == "radio":
                ftype, opts = "radio", b.radios
            elif b.kind == "prompt":
                ftype = "multiselect" if re.search(r"skill", label, re.I) else "select"
            elif b.kind == "checkbox":
                ftype = "checkbox" if b.n_checks > 1 else "boolean"
            elif b.kind == "date":
                ftype = "date"
            elif b.kind == "file":
                ftype, label = "file", "Resume/CV"
            elif b.kind == "textarea":
                ftype = "textarea"
            elif re.search(r"email", b.name, re.I):
                ftype = "email"
            elif re.fullmatch(r"phoneNumber|phone", b.name):
                ftype = "phone"
            fields.append(
                FormField(
                    id=b.name,
                    label=label or b.name,
                    type=ftype,  # type: ignore[arg-type]
                    required=b.required,
                    options=opts,
                    max_length=b.max,
                    section="eeo" if eeo else "main",
                    description=clean_label(b.label) if b.name in CANONICAL else None,
                )
            )
        return StepRead(
            name=name, spec=FormSpec(ats="workday", url=url, fields=fields), boxes=self.boxes
        )

    def _read_listbox(self, page: Any, b: Box) -> list[str]:
        btn = self.box(page, b.name).locator("button[aria-haspopup=listbox]").first
        try:
            self.safe_click(btn)
            texts = self.wait_options(page, 6)
        except Exception as e:
            log.info("workday: couldn't read options of %s: %s", b.name, e)
            return []
        finally:
            self.escape(btn)
            page.wait_for_timeout(200)
        return [t for t in texts if t and not PLACEHOLDER.match(t)]

    # --- the Adapter hooks ------------------------------------------------------------------

    def ready(self, page: Any) -> None:
        self.settle(page, 8_000)

    def page_ids(self, ff: FillField) -> set[str]:
        b = self.boxes.get(ff.id)
        return {ff.id, b.control} if b and b.control else {ff.id}

    def before_fill(self, page: Any, sheet: AnswerSheet) -> list[FieldOutcome]:
        return []

    def order(self, sheet: AnswerSheet) -> list[FillField]:
        # top to bottom: a later question may be revealed by an earlier answer
        idx = {name: b.i for name, b in self.boxes.items()}
        return sorted(sheet.to_fill(), key=lambda f: idx.get(f.id, 10_000))

    def anchor(self, page: Any, ff: FillField) -> Any | None:
        try:
            return self.box(page, ff.id)
        except FieldMissing:
            return None

    def unlisted(self, page: Any, sheet: AnswerSheet) -> list[FieldOutcome]:
        # Workday's required questions are all formField boxes, read above; repeatable sections
        # are filled from the profile. Required controls the step still shows empty are caught
        # by ``check`` before Save and Continue.
        return []

    def set_field(self, page: Any, ff: FillField, sheet: AnswerSheet) -> None:
        b = self.boxes.get(ff.id)
        if b is None:
            raise FieldMissing(ff.id)
        box = self.box(page, ff.id)
        v = ff.value
        if b.kind == "text" or b.kind == "textarea":
            inp = box.locator("textarea" if b.kind == "textarea" else "input").first
            self._type(page, inp, value_text(ff))
        elif b.kind == "listbox":
            self._listbox(page, box, str(v))
        elif b.kind == "radio":
            self._radio(page, box, str(v))
        elif b.kind == "checkbox":
            self._checkbox(page, box, ff)
        elif b.kind == "prompt":
            for one in v if isinstance(v, list) else [str(v)]:
                self._prompt(page, box, one, ff.key)
        elif b.kind == "date":
            m, d, y = parse_ym(str(v))
            if y is None:
                raise NoOption(f"“{v}” isn't a date this field takes")
            self._date(page, box, m, d, y)
        elif b.kind == "file":
            self._upload(page, box, str(v))
        else:
            raise FieldMissing(f"{ff.id}: unknown kind {b.kind}")

    def read_field(self, page: Any, ff: FillField) -> str | None:
        b = self.boxes.get(ff.id)
        if b is None:
            return None
        box = self.box(page, ff.id)
        if b.kind in ("text", "textarea"):
            return box.locator("textarea" if b.kind == "textarea" else "input").first.input_value()
        if b.kind == "listbox":
            t = (box.locator("button[aria-haspopup=listbox]").first.inner_text() or "").strip()
            return None if PLACEHOLDER.match(t) else t
        if b.kind == "radio":
            for i, lab in enumerate(b.radios):
                r = box.locator("input[type=radio]").nth(i)
                if r.count() and r.is_checked():
                    return lab
            return None
        if b.kind == "checkbox":
            cbs = box.locator("input[type=checkbox]")
            labs = [t.strip() for t in box.locator("label").all_inner_texts()]
            if cbs.count() == 1:
                return "Yes" if cbs.first.is_checked() else "No"
            return "; ".join(
                labs[i] for i in range(cbs.count()) if i < len(labs) and cbs.nth(i).is_checked()
            )
        if b.kind == "prompt":
            return "; ".join(self.pills(box)) or None
        if b.kind == "date":
            parts = []
            for p in ("Month", "Day", "Year"):
                loc = box.locator(f"[id$=dateSection{p}-input]")
                if loc.count():
                    parts.append(loc.first.input_value().strip())
            return "/".join(x for x in parts if x) or None
        if b.kind == "file":
            return box.inner_text()
        return None

    def matches(self, ff: FillField, actual: str | None) -> bool:
        b = self.boxes.get(ff.id)
        if b and actual:
            if b.kind == "prompt":
                want = ff.value if isinstance(ff.value, list) else [str(ff.value)]
                got = {norm(x) for x in actual.split(";")}
                return all(
                    any(norm(_SEP.split(w)[-1]) in g or norm(w) in g for g in got) for w in want
                )
            if b.kind == "date":
                m, d, y = parse_ym(str(ff.value))
                return [int(x) for x in actual.split("/") if x.isdigit()] == [
                    x for x in (m, d, y) if x is not None
                ]
            if b.kind == "checkbox" and ff.type == "boolean":
                return norm(actual) == norm(str(ff.value))
        return super().matches(ff, actual)

    # --- per-kind setters -------------------------------------------------------------------

    def _type(self, page: Any, inp: Any, value: str) -> None:
        if inp.input_value().strip() == value.strip():
            return
        self.center(inp)
        inp.fill(value)
        safe_press(inp, "Tab")  # Workday saves a field when it loses focus
        page.wait_for_timeout(250)

    def _listbox(self, page: Any, box: Any, want: str) -> None:
        btn = box.locator("button[aria-haspopup=listbox]").first
        if norm(btn.inner_text()) == norm(want):
            return
        self.safe_click(btn)
        texts = self.wait_options(page)
        pick = pick_option(want, texts)
        if pick is None:
            self.escape(btn)
            raise NoOption(f"no option “{want}” in this dropdown: pick it yourself")
        self.safe_click(page.locator(f"[data-jh-opt='{texts.index(pick)}']").first)
        page.wait_for_timeout(400)

    def _radio(self, page: Any, box: Any, want: str) -> None:
        labs = box.locator("label")
        texts = [t.strip() for t in labs.all_inner_texts()]
        pick = pick_option(want, texts)
        if pick is None:
            raise NoOption(f"no choice “{want}” here: pick it yourself")
        lab = labs.nth(texts.index(pick))
        fid = lab.get_attribute("for")
        target = box.locator(f"input[id='{fid}']").first if fid else lab
        if fid and target.is_checked():
            return
        self.safe_click(target)
        page.wait_for_timeout(300)

    def _checkbox(self, page: Any, box: Any, ff: FillField) -> None:
        cbs = box.locator("input[type=checkbox]")
        if ff.type == "boolean":
            want = norm(str(ff.value)) in ("yes", "true", "checked")
            if cbs.first.is_checked() != want:
                self.safe_click(cbs.first)
                page.wait_for_timeout(300)
            return
        labs = [t.strip() for t in box.locator("label").all_inner_texts()]
        wanted = ff.value if isinstance(ff.value, list) else [str(ff.value)]
        for w in wanted:
            pick = pick_option(w, labs)
            if pick is None:
                raise NoOption(f"no choice “{w}” here: pick it yourself")
            cb = cbs.nth(labs.index(pick))
            if not cb.is_checked():
                self.safe_click(cb)
                page.wait_for_timeout(300)

    def _date(self, page: Any, box: Any, month: int | None, day: int | None, year: int) -> None:
        """Workday's MM / DD / YYYY sections ignore clicks: focus each and type its digits."""
        parts = {p: box.locator(f"[id$=dateSection{p}-input]") for p in ("Month", "Day", "Year")}
        want = {"Month": month, "Day": day, "Year": year}
        for p, loc in parts.items():
            if not loc.count():
                continue
            if want[p] is None:
                raise NoOption(f"this date needs a {p.lower()} your answer doesn't have")
            cur = loc.first.input_value().strip()
            if cur.isdigit() and int(cur) == want[p]:
                continue
            self.center(loc.first)
            loc.first.focus()
            loc.first.press_sequentially(f"{want[p]:02d}" if p != "Year" else str(year), delay=60)
            page.wait_for_timeout(120)
        safe_press(parts["Year"].first, "Tab")
        page.wait_for_timeout(300)

    def _upload(self, page: Any, box: Any, path: str) -> None:
        name = Path(path).name
        if name in box.inner_text():
            return
        fi = box.locator("input[type=file]").first
        self.check_accept(fi, path)
        fi.set_input_files(path)
        end = time.monotonic() + max(self.parse_wait_s, 10)
        while time.monotonic() < end:
            page.wait_for_timeout(500)
            text = box.inner_text()
            if name in text:
                return
            if re.search(r"error|failed|not supported|too large", text, re.I):
                raise NoOption(f"Workday didn't take the file: {text[:120]}")
        raise NoOption("the upload didn't finish")

    def _prompt(self, page: Any, box: Any, want: str, key: str | None) -> None:
        """A search prompt: type, search (Enter only in Workday's search box) or browse
        categories and long lists, and pick the one option that matches ``want``. A value with a
        separator ("Campus: Career Fair") names its category first."""
        picker = BUILTIN[key].pick if key and key in BUILTIN and BUILTIN[key].pick else None

        def choose(texts: list[str]) -> str | None:
            real = [t for t in texts if t and not PLACEHOLDER.match(t)]
            if picker and (hit := picker(want, real)):
                return hit
            if hit := pick_option(want, real):
                return hit
            tail = _SEP.split(want)[-1]
            hits = [t for t in real if norm(_SEP.split(t)[-1]) == norm(tail)]
            return hits[0] if len({norm(h) for h in hits}) == 1 else None

        if any(choose([p]) for p in self.pills(box)):
            return
        inp = box.locator("input:not([type=hidden])").first
        self.safe_click(inp)
        texts = self.wait_options(page, 3)
        hit = choose(texts)
        parts = _SEP.split(want)
        if hit is None and len(parts) > 1 and (cat := pick_option(parts[0], texts)):
            self.safe_click(page.locator(f"[data-jh-opt='{texts.index(cat)}']").first)
            texts = self.wait_options(page, 4)
            hit = choose(texts)
        if hit is None:
            inp.fill(_SEP.split(want)[-1])
            texts = self.wait_options(page, 2)
            hit = choose(texts)
            if hit is None:
                with contextlib.suppress(SubmitRefused):
                    search_enter(inp)
                    texts = self.wait_options(page, 6)
                    hit = choose(texts)
        if hit is None:
            inp.fill("")
            page.wait_for_timeout(500)
            texts = self.wait_options(page, 2)
            if "All" in texts:
                self.safe_click(page.locator(f"[data-jh-opt='{texts.index('All')}']").first)
                end = time.monotonic() + 6
                while time.monotonic() < end and len(self.options(page)) <= 2:
                    page.wait_for_timeout(250)
            for _ in range(300):
                texts = self.options(page)
                hit = choose(texts)
                if hit or not page.evaluate(SCROLL_JS):
                    break
                page.wait_for_timeout(120)
        if hit is None:
            self.escape(inp)
            raise NoOption(f"no option matches “{want}” confidently: pick it yourself")
        for _ in range(3):  # a hit can be a category ("Job Board"): look among its children
            texts = self.options(page)
            self.safe_click(page.locator(f"[data-jh-opt='{texts.index(hit)}']").first)
            page.wait_for_timeout(600)
            if any(choose([p]) for p in self.pills(box)):
                break
            children = self.options(page)
            if not children or children == texts:
                break
            hit = choose([c for c in children if norm(c) != norm(hit)])
            if hit is None:
                self.escape(inp)
                raise NoOption(f"“{want}” is a category here; none of its options matches")
        if self.options(page):
            self.escape(inp)

    # --- My Experience: repeatable sections from the profile --------------------------------

    def entries(self, page: Any, kind: str) -> dict[str, list[dict[str, Any]]]:
        return page.evaluate(ENTRIES_JS, kind)

    def add_entry(self, page: Any, kind: str, title: str) -> str | None:
        before = set(self.entries(page, kind))
        if not page.evaluate(ADD_JS, title):
            return None
        self.safe_click(page.locator("[data-jh-add='1']").first)
        end = time.monotonic() + 6
        while time.monotonic() < end:
            page.wait_for_timeout(300)
            new = [p for p in self.entries(page, kind) if p not in before]
            if new:
                return new[0]
        return None

    def fill_entry(
        self, page: Any, prefix: str, values: list[tuple[str, str, Any, str | None]], section: str
    ) -> list[FieldOutcome]:
        """``values``: (key regex, label, value, bank key). Each control matched by its id key
        or its label; value None leaves the part alone."""
        ctrls = self.entries(page, prefix.split("-")[0]).get(prefix, [])
        out: list[FieldOutcome] = []
        done: set[str] = set()
        for rx, label, value, key in values:
            if value in (None, "", []):
                continue
            c = next(
                (c for c in ctrls if c["key"] not in done
                 and (re.search(rx, c["key"], re.I) or re.search(rx, c["label"], re.I))),
                None,
            )  # fmt: skip
            fid = f"{prefix}/{label}"
            ff = FillField(id=fid, label=f"{section}: {label}", type="text", required=False,
                           section="main", key=key, action="fill", value=str(value))  # fmt: skip
            if c is None:
                continue  # this employer's section doesn't ask for it
            base = re.sub(r"-dateSection(Month|Day|Year)-input$", "", c["key"])
            done |= {x["key"] for x in ctrls if x["key"].startswith(base)}
            status, note = "filled", None
            try:
                box = (
                    page.locator(f"[id='{c['id']}']")
                    .locator("xpath=ancestor::*[starts-with(@data-automation-id,'formField-')][1]")
                    .first
                )
                if "dateSection" in c["key"]:
                    m, d, y = parse_ym(str(value))
                    if y is None:
                        raise NoOption("no year in this date")
                    self._date(page, box, m, d, y)
                elif c["tag"] == "button" or c["popup"] == "listbox":
                    self._listbox_pick(page, box, str(value), key)
                elif c["prompt"]:
                    self._prompt(page, box, str(value), key)
                elif c["key"].lower().startswith("currently") or "check" in c["key"].lower():
                    cb = page.locator(f"[id='{c['id']}']").first
                    if cb.is_checked() != bool(value):
                        self.safe_click(cb)
                        page.wait_for_timeout(300)
                else:
                    self._type(page, page.locator(f"[id='{c['id']}']").first, str(value))
            except SubmitRefused as e:
                status, note = "refused", str(e)
            except (NoOption, FieldMissing) as e:
                status, note = "no_option", str(e)
            except Exception as e:
                log.warning("workday: %s %s failed: %s", section, label, e)
                status, note = "error", f"couldn't set this: {type(e).__name__}"
            o = self._outcome(ff, status, note)
            out.append(o)
        return out

    def _listbox_pick(self, page: Any, box: Any, want: str, key: str | None) -> None:
        btn = box.locator("button[aria-haspopup=listbox]").first
        cur = (btn.inner_text() or "").strip()
        self.safe_click(btn)
        texts = [t for t in self.wait_options(page) if not PLACEHOLDER.match(t)]
        picker = BUILTIN[key].pick if key and key in BUILTIN and BUILTIN[key].pick else None
        pick = (picker(want, texts) if picker else None) or pick_option(want, texts)
        if pick is None:
            self.escape(btn)
            raise NoOption(f"no option matches “{want}”: pick it yourself")
        if norm(pick) == norm(cur):
            self.escape(btn)
            return
        all_texts = self.options(page)
        self.safe_click(page.locator(f"[data-jh-opt='{all_texts.index(pick)}']").first)
        page.wait_for_timeout(400)

    def fill_experience(self, page: Any, ctx: Any) -> list[FieldOutcome]:
        """Work, education, languages and websites, from the profile and answers only. Entries
        already on the page (a draft, Workday's resume parse) are matched, not duplicated."""
        out: list[FieldOutcome] = []
        prof, ans = ctx.profile, ctx.answers
        # work experience
        jobs = list(prof.experience)
        have = self.entries(page, "workExperience")
        prefixes = sorted(have, key=lambda p: int(p.split("-")[1]))
        filled = {p: self._entry_text(page, have[p]) for p in prefixes}
        free = [p for p in prefixes if not filled[p].strip()]
        for job in jobs:
            sig = norm(job.org)
            if any(sig and sig in norm(t) and norm(job.title) in norm(t) for t in filled.values()):
                continue
            p = free.pop(0) if free else self.add_entry(page, "workExperience", "Work Experience")
            if p is None:
                break
            out += self.fill_entry(page, p, [
                (r"jobTitle", "Job title", job.title, None),
                (r"companyName", "Company", job.org, None),
                (r"location", "Location", getattr(job, "location", None), None),
                (r"currentlyWorkHere", "I currently work here", job.end is None, None),
                (r"startDate", "From", job.start, None),
                (r"endDate", "To", job.end, None),
                (r"roleDescription", "Role description", "\n".join(job.bullets or []) or None, None),
            ], "Work Experience")  # fmt: skip
        # education
        have = self.entries(page, "education")
        prefixes = sorted(have, key=lambda p: int(p.split("-")[1]))
        filled = {p: self._entry_text(page, have[p]) for p in prefixes}
        free = [p for p in prefixes if not filled[p].strip()]
        for n, ed in enumerate(prof.education):
            if any(norm(ed.school) in norm(t) for t in filled.values()):
                continue
            p = free.pop(0) if free else self.add_entry(page, "education", "Education")
            if p is None:
                break
            gpa = BUILTIN["education.gpa"].resolve(ctx) if n == 0 else None
            out += self.fill_entry(page, p, [
                (r"^school", "School", ed.school, "education.school"),
                (r"degree", "Degree", ed.degree, "education.degree"),
                (r"fieldOfStudy|major", "Field of study", (ed.majors or [None])[0], "education.major"),
                (r"grade|gpa", "GPA", gpa, "education.gpa"),
                (r"firstYear|startDate", "From", ed.start, None),
                (r"lastYear|endDate", "To", ed.graduation, None),
            ], "Education")  # fmt: skip
        # languages: the language itself; proficiency scales differ per employer and stay yours
        have = self.entries(page, "language")
        known = {norm(self._entry_text(page, v)) for v in have.values()}
        free = [p for p, v in have.items() if not self._entry_text(page, v).strip()]
        for lang in ans.skills.spoken_languages:
            if any(norm(lang) in k for k in known):
                continue
            p = free.pop(0) if free else self.add_entry(page, "language", "Languages")
            if p is None:
                break
            out += self.fill_entry(
                page, p, [(r"^language$", "Language", lang, "skills.languages")], "Languages"
            )
        # websites
        links = [x for x in (ans.links.github, ans.links.portfolio) if x]
        have = self.entries(page, "webAddress")
        known = {norm(self._entry_text(page, v)) for v in have.values()}
        free = [p for p, v in have.items() if not self._entry_text(page, v).strip()]
        for url in links:
            if norm(url) in known:
                continue
            p = free.pop(0) if free else self.add_entry(page, "webAddress", "Websites")
            if p is None:
                break
            out += self.fill_entry(page, p, [(r"url", "URL", url, None)], "Websites")
        return out

    @staticmethod
    def _entry_text(page: Any, ctrls: list[dict[str, Any]]) -> str:
        vals = []
        for c in ctrls:
            loc = page.locator(f"[id='{c['id']}']").first
            with contextlib.suppress(Exception):
                if c["tag"] in ("input", "textarea") and "dateSection" not in c["key"]:
                    if (loc.get_attribute("type") or "") not in ("checkbox", "radio"):
                        vals.append(loc.input_value())
                elif c["tag"] == "button":
                    t = loc.inner_text().strip()
                    vals.append("" if PLACEHOLDER.match(t) else t)
            if c["prompt"]:
                box = loc.locator(
                    "xpath=ancestor::*[starts-with(@data-automation-id,'formField-')][1]"
                )
                vals += Workday.pills(box)
        return " ".join(v for v in vals if v)
