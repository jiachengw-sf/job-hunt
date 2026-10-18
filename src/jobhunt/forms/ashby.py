"""Ashby: the form-definition API needs employer auth and the hosted page builds its form in
JavaScript, so the page is loaded headlessly and the form definition the page itself fetches
(``ApiJobPosting``) is read from its network traffic.

The browser never clicks, types or submits. Every request the page makes is routed through a
guard that allows only GETs and the page's two read-only queries; anything else is aborted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from jobhunt.discovery.sources import _ashby_pay
from jobhunt.discovery.text import html_to_text
from jobhunt.forms.model import FieldType, FormError, FormField, FormSpec, one_line
from jobhunt.http import HttpClient, TransportError

READ_OPS = frozenset({"ApiJobPosting", "ApiOrganizationFromHostedJobsPageName"})
GRAPHQL = "/api/non-user-graphql"

_TYPES: dict[str, FieldType] = {
    "String": "text",
    "Email": "email",
    "Phone": "phone",
    "File": "file",
    "LongText": "textarea",
    "Boolean": "boolean",
    "ValueSelect": "select",
    "MultiValueSelect": "multiselect",
    "Location": "location",
    "Date": "date",
    "Number": "number",
    "SocialLink": "url",
}


def application_url(board: str, job_id: str) -> str:
    return f"https://jobs.ashbyhq.com/{board}/{job_id}/application"


def _fields(sections: list[dict[str, Any]], section: str) -> list[FormField]:
    out: list[FormField] = []
    for sec in sections or []:
        for fe in sec.get("fieldEntries") or []:
            f = fe.get("field") or {}
            if fe.get("isHidden") or f.get("isDeactivated"):
                continue
            opts = [
                str(v.get("label"))
                for v in f.get("selectableValues") or []
                if v.get("label") is not None
            ]
            out.append(
                FormField(
                    id=str(f.get("path") or f.get("id")),
                    label=one_line(f.get("title")) or str(f.get("path")),
                    description=html_to_text(fe.get("descriptionHtml")) or None,
                    type=_TYPES.get(str(f.get("type")), "text"),
                    required=bool(fe.get("isRequired")),
                    options=opts,
                    section=section,  # type: ignore[arg-type]
                )
            )
    return out


def parse(posting: Any, url: str | None = None) -> FormSpec:
    """Parse the ``jobPosting`` object of the page's ApiJobPosting response."""
    if not isinstance(posting, dict) or not isinstance(posting.get("applicationForm"), dict):
        raise FormError("Ashby posting has no application form")
    form = posting["applicationForm"]
    fields = _fields(form.get("sections") or [], "main")
    for sf in posting.get("surveyForms") or []:
        title = " ".join(
            html_to_text(s.get("descriptionHtml")) or "" for s in sf.get("sections") or []
        )
        section = (
            "eeo" if re.search(r"equal employment|eeo|self.identif", title, re.I) else "survey"
        )
        fields += _fields(sf.get("sections") or [], section)
    if posting.get("shouldAskForTextingConsent"):
        fields.append(
            FormField(
                id="communicationConsent",
                label="Do you consent to receiving text messages?",
                type="radio",
                options=["Yes", "No"],
                section="consent",
            )
        )
    if not fields:
        raise FormError("Ashby form has no fields")
    comp = {
        "compensationTierSummary": posting.get("compensationTierSummary")
        or posting.get("scrapeableCompensationSalarySummary")
    }
    return FormSpec(
        ats="ashby",
        url=url,
        fields=fields,
        captcha_at_submit=True,  # Ashby runs reCAPTCHA on submit
        description=html_to_text(posting.get("descriptionHtml")),
        pay=_ashby_pay(comp) if comp["compensationTierSummary"] else None,
    )


class Loader(Protocol):
    def __call__(self, board: str, job_id: str) -> dict[str, Any]: ...


def _allowed(method: str, url: str) -> bool:
    if method in ("GET", "HEAD"):
        return True
    if method == "POST" and GRAPHQL in url:
        m = re.search(r"[?&]op=([A-Za-z]+)", url)
        return bool(m and m.group(1) in READ_OPS)
    return False


def _failing(message: str, status: str) -> Loader:
    def load(board: str, job_id: str) -> dict[str, Any]:
        raise FormError(message, status)

    return load


@contextmanager
def browser_session(timeout_s: float = 45.0, channel: str | None = "chrome") -> Iterator[Loader]:
    """One headless browser for a run; each load opens a page and reads the page's own
    ApiJobPosting response. Yields a loader that fails per job when no browser is available."""
    try:
        from playwright.sync_api import Error as PwError
        from playwright.sync_api import sync_playwright
    except ImportError:
        yield _failing(
            "Reading Ashby forms needs the browser extra: `uv tool install 'jobhunt[browser]'`",
            "needs_browser",
        )
        return

    def guard(route: Any) -> None:
        req = route.request
        if _allowed(req.method, req.url):
            route.continue_()
        else:
            route.abort()

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel=channel, headless=True)
        except PwError:
            try:
                browser = p.chromium.launch(headless=True)
            except PwError:
                yield _failing(
                    "No browser for Playwright: run `jobhunt doctor --fix`", "needs_browser"
                )
                return

        def load(board: str, job_id: str) -> dict[str, Any]:
            got: dict[str, Any] = {}

            def on_response(resp: Any) -> None:
                if GRAPHQL in resp.url and "op=ApiJobPosting" in resp.url:
                    try:
                        got["posting"] = (resp.json().get("data") or {}).get("jobPosting")
                        got["seen"] = True
                    except Exception:
                        got["error"] = "unreadable form response"

            page = browser.new_page()
            try:
                page.route("**/*", guard)
                page.on("response", on_response)
                page.goto(
                    application_url(board, job_id),
                    wait_until="networkidle",
                    timeout=timeout_s * 1000,
                )
            except PwError as e:
                raise FormError(f"Ashby page didn't load: {str(e).splitlines()[0]}") from e
            finally:
                page.close()
            if got.get("seen") and got.get("posting") is None:
                raise FormError("Ashby says this posting no longer exists", "not_found")
            if not got.get("posting"):
                raise FormError(got.get("error") or "Ashby page loaded no application form")
            return got["posting"]

        try:
            yield load
        finally:
            browser.close()


@contextmanager
def fixture_session(client: HttpClient) -> Iterator[Loader]:
    """Offline: ``jobs.ashbyhq.com/<board>/<id>/form.json`` holds a saved jobPosting object."""

    def load(board: str, job_id: str) -> dict[str, Any]:
        try:
            resp = client.get(f"https://jobs.ashbyhq.com/{board}/{job_id}/form")
        except TransportError as e:
            raise FormError(str(e)) from e
        if resp.status == 404:
            raise FormError("Ashby says this posting no longer exists", "not_found")
        data = json.loads(resp.body)
        return data.get("jobPosting", data)

    yield load


def fetch(
    loader: Loader | Callable[[str, str], dict[str, Any]], board: str, job_id: str
) -> FormSpec:
    posting = loader(board, job_id)
    if posting is None:
        raise FormError("Ashby says this posting no longer exists", "not_found")
    return parse(posting, application_url(board, job_id))
