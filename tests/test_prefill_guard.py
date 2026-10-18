"""The submit guard: all three layers. jobhunt never submits an application."""

from __future__ import annotations

import re

import pytest

from jobhunt.cli.main import cli as cli_group
from jobhunt.prefill.guard import RouteGuard, SubmitRefused, refusal, safe_click, safe_press
from tests.conftest import REPO

PREFILL = REPO / "src" / "jobhunt" / "prefill"

# --- layer 1: no submit command, and no other way to click -----------------------------------


def _all_params(group) -> list[str]:
    out = []
    for name, cmd in group.commands.items():
        out.append(name)
        out += [o for p in cmd.params for o in getattr(p, "opts", [])]
        if hasattr(cmd, "commands"):
            out += _all_params(cmd)
    return out


def test_no_submit_command_or_option():
    words = " ".join(_all_params(cli_group)).lower()
    assert "submit" not in words
    assert not re.search(r"\bauto-?apply\b", words)


def test_engine_clicks_only_through_the_wrapper():
    """Adapters may not click, press, submit or dispatch events except via the guard."""
    banned = re.compile(
        r"\.(click|dblclick|tap|press|check|uncheck|dispatch_event|set_checked)\(|"
        r"requestSubmit|\.submit\(\)|keyboard\.press"
    )
    offenders = []
    for py in PREFILL.rglob("*.py"):
        if py.name == "guard.py":
            continue
        for n, line in enumerate(py.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if banned.search(code):
                offenders.append(f"{py.relative_to(REPO)}:{n}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


# --- layer 2: the click wrapper ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("el", "refused"),
    [
        ({"tag": "button", "type": "submit", "text": "Next"}, True),
        ({"tag": "input", "type": "submit", "value": "Go"}, True),
        ({"tag": "input", "type": "image"}, True),
        ({"tag": "button", "type": "", "text": "Attach", "in_form": True}, True),
        ({"tag": "button", "type": "button", "text": "Submit application"}, True),
        ({"tag": "a", "text": "Apply now"}, True),
        ({"tag": "div", "role": "button", "text": "Send"}, True),
        ({"tag": "button", "type": "button", "aria_label": "Finish"}, True),
        ({"tag": "button", "type": "button", "text": "Complete your application"}, True),
        ({"tag": "button", "type": "button", "text": "Attach"}, False),
        ({"tag": "button", "type": "", "text": "Yes", "in_form": False}, False),
        ({"tag": "div", "role": "option", "text": "Complete"}, False),  # an option, not a button
        ({"tag": "input", "type": "text"}, False),
        ({"tag": "button", "type": "button", "text": "Applied Physics"}, False),
    ],
)
def test_refusal_rules(el, refused):
    assert bool(refusal(el)) is refused


def test_enter_is_refused():
    class Dummy:
        pressed: list[str] = []

        def press(self, key):
            self.pressed.append(key)

    d = Dummy()
    for key in ("Enter", "NumpadEnter", "Shift+Enter"):
        with pytest.raises(SubmitRefused):
            safe_press(d, key)
    safe_press(d, "ArrowDown")
    assert d.pressed == ["ArrowDown"]


PAGE = """<!doctype html><html><body>
<form id="f" action="https://ats.test/submit" method="post">
  <input id="name" name="name">
  <button type="button" id="attach">Attach</button>
  <button id="plain">Yes</button>
  <button type="submit" id="go"><span id="inner">Submit application</span></button>
  <input type="submit" id="isub" value="Go">
  <a href="#" id="apply">Apply now</a>
  <div role="option" id="opt">Complete</div>
  <button type="button" id="beacon">Upload</button>
</form>
<script>
  window.clicks = [];
  document.addEventListener('click', e => window.clicks.push(e.target.id));
  document.getElementById('f').addEventListener('submit', e => { window.submitted = true; });
  document.getElementById('beacon').addEventListener('click', () => {
    fetch('https://ats.test/upload', {method: 'POST', body: 'file'});
    fetch('https://ats.test/submit', {method: 'POST', body: 'sneaky'});
  });
</script></body></html>"""


@pytest.fixture
def ats(browser_context):
    """A fake ATS at https://ats.test that records every request reaching it."""
    seen: list[tuple[str, str]] = []

    def server(route):
        req = route.request
        seen.append((req.method, req.url))
        if req.url.endswith("/form"):
            route.fulfill(status=200, content_type="text/html", body=PAGE)
        elif req.url.endswith("/submit"):
            route.fulfill(status=200, content_type="text/html", body="<p>Thanks</p>")
        else:
            route.fulfill(status=204, body="")

    browser_context.route("https://ats.test/**", server)
    page = browser_context.new_page()
    return page, seen


@pytest.mark.parametrize("target", ["#go", "#inner", "#isub", "#apply", "#plain"])
def test_wrapper_refuses_submit_like_elements(ats, target):
    page, seen = ats
    page.goto("https://ats.test/form")
    with pytest.raises(SubmitRefused):
        safe_click(page.locator(target))
    assert page.evaluate("window.clicks") == []
    assert not page.evaluate("window.submitted || false")
    assert not [s for s in seen if s[1].endswith("/submit")]


def test_wrapper_allows_ordinary_controls(ats):
    page, _ = ats
    page.goto("https://ats.test/form")
    safe_click(page.locator("#attach"))
    safe_click(page.locator("#opt"))
    assert page.evaluate("window.clicks") == ["attach", "opt"]


# --- layer 3: the route guard -----------------------------------------------------------------


def test_guard_permits():
    g = RouteGuard.of(
        allow=[r"^POST https://ats\.test/upload"], submit=[r"^POST https://ats\.test/sub"]
    )
    assert g.permits("GET", "https://ats.test/form")
    assert g.permits("POST", "https://ats.test/upload")
    assert not g.permits("POST", "https://ats.test/submit")
    assert not g.permits("PUT", "https://elsewhere.test/x")  # default deny
    assert not g.permits("POST", "https://analytics.test/collect")
    g.active = False
    assert g.permits("POST", "https://ats.test/submit")


def test_submit_pattern_beats_allow():
    g = RouteGuard.of(
        allow=[r"^POST https://ats\.test/"], submit=[r"^POST https://ats\.test/submit"]
    )
    assert g.permits("POST", "https://ats.test/upload")
    assert not g.permits("POST", "https://ats.test/submit")


def test_no_submit_request_leaves_during_fill_then_human_can_submit(ats):
    page, seen = ats
    guard = RouteGuard.of(
        allow=[r"^POST https://ats\.test/upload$"], submit=[r"^POST https://ats\.test/submit"]
    )
    guard.install(page)
    page.goto("https://ats.test/form")
    page.fill("#name", "Test Person")
    safe_click(page.locator("#beacon"))  # page script fires an upload and a sneaky submit
    page.wait_for_timeout(300)
    page.evaluate("document.getElementById('f').submit()")  # even a scripted form submit
    page.wait_for_timeout(300)
    assert ("POST", "https://ats.test/upload") in seen
    assert not [s for s in seen if s[1].endswith("/submit")], seen
    assert {b["url"] for b in guard.blocked} == {"https://ats.test/submit"}

    guard.release(page)  # handoff: the human clicks Submit themselves
    page.goto("https://ats.test/form")
    with page.expect_navigation():
        page.locator("#go").click()  # the human's click, not the engine's
    assert ("POST", "https://ats.test/submit") in seen
