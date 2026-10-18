"""The offline router behind `prefill --fixtures`: pages and endpoints from disk, no network."""

from __future__ import annotations

import contextlib
import json

from jobhunt.prefill.fixtures import FixtureRouter


def test_router_serves_pages_and_endpoints(browser_context, tmp_path):
    (tmp_path / "ats.test" / "jobs").mkdir(parents=True)
    (tmp_path / "ats.test" / "jobs" / "1.html").write_text(
        "<!doctype html><p id=x>job one</p>"
        "<script>fetch('/api/q?op=Read',{method:'POST'}).then(r=>r.json())"
        ".then(j=>document.title=j.ok);fetch('/nothing',{method:'POST'})</script>"
    )
    (tmp_path / "ats.test" / "api" / "q").mkdir(parents=True)
    (tmp_path / "ats.test" / "api" / "q" / "Read.POST.json").write_text('{"json": {"ok": "yes"}}')
    log = tmp_path / "requests.jsonl"
    router = FixtureRouter(tmp_path, log_path=log)
    router.install(browser_context)
    page = browser_context.new_page()
    page.goto("https://ats.test/jobs/1")
    page.wait_for_function("document.title === 'yes'")
    assert page.inner_text("#x") == "job one"
    seen = {(r["method"], r["url"].split("ats.test")[1], r["status"]) for r in router.requests}
    assert ("GET", "/jobs/1", "200") in seen
    assert ("POST", "/api/q?op=Read", "200") in seen
    assert ("POST", "/nothing", "404") in seen
    assert len(log.read_text().splitlines()) == len(router.requests)
    assert json.loads(log.read_text().splitlines()[0])["method"] == "GET"


def test_router_never_reaches_the_network(browser_context, tmp_path):
    (tmp_path / "ats.test").mkdir()
    router = FixtureRouter(tmp_path)
    router.install(browser_context)
    page = browser_context.new_page()
    with contextlib.suppress(Exception):
        page.goto("https://example.com/", timeout=5_000)
    assert router.requests[0]["status"] == "0"  # aborted, not fetched
