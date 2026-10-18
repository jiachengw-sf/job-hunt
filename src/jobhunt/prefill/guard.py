"""The submit guard. jobhunt never submits an application; three layers make sure of it.

1. No ``submit`` command exists anywhere in the CLI (a contract test checks every command).
2. Every click the engine makes goes through :func:`safe_click`, which refuses ``type=submit``
   elements, ``<button>``s without a type inside a form (they submit it), and buttons or links
   whose text says submit / apply / send / finish / complete. Pressing Enter in a field (implicit
   submission) is refused too. A source test checks that adapters never click any other way.
3. While the engine drives a tab, :class:`RouteGuard` aborts every request that could change
   state: any method other than GET/HEAD/OPTIONS is blocked unless the adapter allow-lists it
   (resume uploads, read-only queries), and the adapter's known submit endpoints are blocked
   even if an allow pattern matches. The guard is released only at handoff, after the engine
   stops touching the page, so the human can submit.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

SUBMIT_TEXT = re.compile(r"\b(submit|apply|send|finish|complete)\b", re.I)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_REFUSED_KEYS = frozenset({"Enter", "NumpadEnter"})

# The element a click lands on: the nearest button-like ancestor, else the element itself.
_CLICK_INFO_JS = """el => {
  const t = el.closest('button, a, [role=button], [role=link], input[type=submit],'
    + ' input[type=button], input[type=image], input[type=reset]') || el;
  return {
    tag: t.tagName.toLowerCase(),
    type: (t.getAttribute('type') || '').toLowerCase(),
    role: (t.getAttribute('role') || '').toLowerCase(),
    text: (t.innerText || t.textContent || '').trim().slice(0, 200),
    aria_label: t.getAttribute('aria-label') || '',
    value: t.tagName === 'INPUT' ? (t.value || '') : '',
    title: t.getAttribute('title') || '',
    in_form: !!(t.form || t.closest('form')),
  };
}"""


class SubmitRefused(Exception):
    """The engine was about to click something that could submit the application."""


def refusal(el: dict[str, Any]) -> str | None:
    """Why clicking this element is refused, or None. ``el`` is what _CLICK_INFO_JS returns."""
    tag, typ, role = el.get("tag", ""), el.get("type", ""), el.get("role", "")
    if typ == "submit" or (tag == "input" and typ == "image"):
        return "type=submit element"
    if tag == "button" and not typ and el.get("in_form"):
        return "a <button> without a type submits its form"
    buttonish = (
        tag in ("button", "a")
        or (tag == "input" and typ in ("button", "reset"))
        or role in ("button", "link", "menuitem")
    )
    if buttonish:
        words = " ".join(str(el.get(k) or "") for k in ("text", "aria_label", "value", "title"))
        if m := SUBMIT_TEXT.search(words):
            return f"button says “{m.group(0)}”"
    return None


def safe_click(locator: Any, **kwargs: Any) -> None:
    """The only way the engine clicks. Raises SubmitRefused instead of clicking a submit."""
    info = locator.evaluate(_CLICK_INFO_JS)
    if why := refusal(info):
        log.warning("refused a click: %s", why)
        raise SubmitRefused(why)
    locator.click(**kwargs)


def safe_press(locator: Any, key: str) -> None:
    """Press a key in a field. Enter is refused: in a form field it submits the form."""
    if key in _REFUSED_KEYS or key.split("+")[-1] in _REFUSED_KEYS:
        raise SubmitRefused("Enter in a form field submits the form")
    locator.press(key)


def _compile(patterns: Iterable[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.I) for p in patterns]


@dataclass
class RouteGuard:
    """Blocks state-changing requests from one tab while the engine fills it.

    Patterns match ``"METHOD URL"``. ``allow`` lists the non-GET requests a fill needs (uploads,
    read-only GraphQL queries); ``submit`` lists the adapter's submit endpoints, which stay
    blocked whatever ``allow`` says.
    """

    allow: list[re.Pattern[str]] = field(default_factory=list)
    submit: list[re.Pattern[str]] = field(default_factory=list)
    blocked: list[dict[str, str]] = field(default_factory=list)
    active: bool = True

    @classmethod
    def of(cls, allow: Iterable[str] = (), submit: Iterable[str] = ()) -> RouteGuard:
        return cls(allow=_compile(allow), submit=_compile(submit))

    def permits(self, method: str, url: str) -> bool:
        if not self.active:
            return True
        key = f"{method.upper()} {url}"
        if any(p.search(key) for p in self.submit):
            return False
        if method.upper() in SAFE_METHODS:
            return True
        return any(p.search(key) for p in self.allow)

    def _handle(self, route: Any) -> None:
        req = route.request
        if self.permits(req.method, req.url):
            route.fallback()
            return
        self.blocked.append({"method": req.method, "url": req.url})
        log.info("blocked %s %s during fill", req.method, req.url)
        route.abort("blockedbyclient")

    def install(self, page: Any) -> None:
        self.active = True
        page.route("**/*", self._handle)

    def release(self, page: Any) -> None:
        """Handoff: the engine has stopped; the human may now submit."""
        self.active = False
        if not page.is_closed():
            page.unroute("**/*", self._handle)
