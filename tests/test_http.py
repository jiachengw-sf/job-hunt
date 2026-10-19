"""Request hygiene: per-host throttle, conditional GETs, Retry-After and jittered backoff."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest

from jobhunt.http import (
    FixtureTransport,
    HttpClient,
    Response,
    TransportError,
    retry_after_seconds,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def scripted(*responses):
    """A transport that returns (or raises) the given items in order and records requests."""
    calls: list[tuple[str, dict[str, str]]] = []
    queue = list(responses)

    def transport(url, headers, timeout):
        calls.append((url, dict(headers)))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return Response(url=url, status=item[0], headers=item[1] if len(item) > 1 else {})

    transport.calls = calls  # type: ignore[attr-defined]
    return transport


def client(transport, clock, **kw):
    return HttpClient(
        user_agent="jobhunt-test",
        transport=transport,
        clock=clock.now,
        sleep=clock.sleep,
        rng=lambda: 0.5,
        **kw,
    )


def test_one_second_gap_per_host_not_across_hosts():
    clock = FakeClock()
    t = scripted((200,), (200,), (200,))
    c = client(t, clock)
    c.get("https://a.example/1")
    c.get("https://b.example/1")  # other host: no wait
    assert clock.sleeps == []
    clock.t += 0.25
    c.get("https://a.example/2")  # same host 0.25s later: waits the rest of the second
    assert clock.sleeps == [pytest.approx(0.75)]


def test_conditional_headers_and_304():
    clock = FakeClock()
    t = scripted((304, {"etag": '"abc"'}))
    resp = client(t, clock).get(
        "https://a.example/x", etag='"abc"', last_modified="Mon, 01 Jan 2026"
    )
    assert resp.not_modified
    headers = t.calls[0][1]
    assert headers["If-None-Match"] == '"abc"'
    assert headers["If-Modified-Since"] == "Mon, 01 Jan 2026"
    assert headers["User-Agent"] == "jobhunt-test"


def test_retry_after_seconds_is_honored():
    clock = FakeClock()
    t = scripted((429, {"retry-after": "7"}), (200,))
    resp = client(t, clock).get("https://a.example/x")
    assert resp.status == 200
    assert 7 in clock.sleeps


def test_backoff_with_jitter_on_5xx_then_gives_up():
    clock = FakeClock()
    t = scripted((503,), (502,), (500,))
    resp = client(t, clock, max_retries=2, backoff_base=1.0).get("https://a.example/x")
    assert resp.status == 500  # final response returned after max_retries
    # rng=0.5 -> factor 1.0: delays 1, 2 (plus any throttle waits, which are 0 after a backoff)
    backoffs = [s for s in clock.sleeps if s >= 1]
    assert backoffs == [1.0, 2.0]


def test_jitter_varies_delay():
    c = HttpClient(user_agent="x", rng=lambda: 0.0)
    assert c._backoff(2) == pytest.approx(2.0)  # 1 * 4 * 0.5
    c.rng = lambda: 0.999
    assert c._backoff(2) == pytest.approx(5.996)


def test_huge_retry_after_is_not_waited_out():
    clock = FakeClock()
    t = scripted((429, {"retry-after": "3600"}), (200,))
    resp = client(t, clock).get("https://a.example/x")
    assert resp.status == 429 and len(t.calls) == 1


def test_network_errors_retry_then_raise():
    clock = FakeClock()
    t = scripted(TransportError("down"), TransportError("down"))
    with pytest.raises(TransportError):
        client(t, clock, max_retries=1).get("https://a.example/x")
    assert len(t.calls) == 2


def test_no_retry_on_404():
    clock = FakeClock()
    t = scripted((404,))
    assert client(t, clock).get("https://a.example/x").status == 404
    assert len(t.calls) == 1


def test_retry_after_parsing():
    assert retry_after_seconds("12") == 12
    assert retry_after_seconds(None) is None
    assert retry_after_seconds("soon") is None
    future = datetime.now(UTC) + timedelta(seconds=30)
    assert 25 <= retry_after_seconds(format_datetime(future, usegmt=True)) <= 31


def test_fixture_transport_serves_files_with_etags(tmp_path: Path):
    f = tmp_path / "api.example" / "v1" / "jobs.json"
    f.parent.mkdir(parents=True)
    f.write_text('{"jobs": []}', encoding="utf-8")
    t = FixtureTransport(tmp_path)
    r1 = t("https://api.example/v1/jobs?content=true", {}, 1)
    assert r1.status == 200 and r1.json() == {"jobs": []}
    r2 = t("https://api.example/v1/jobs", {"If-None-Match": r1.headers["etag"]}, 1)
    assert r2.status == 304
    assert t("https://api.example/v1/missing", {}, 1).status == 404


def test_fixture_transport_serves_html_pages(tmp_path: Path):
    page = tmp_path / "jobs.example" / "acme" / "123" / "apply.html"
    page.parent.mkdir(parents=True)
    page.write_text("<form></form>", encoding="utf-8")
    t = FixtureTransport(tmp_path)
    r = t("https://jobs.example/acme/123/apply?src=x", {}, 1)
    assert r.status == 200 and r.body == b"<form></form>"


def test_accept_header_is_configurable():
    seen: list[dict[str, str]] = []

    def transport(url: str, headers: dict[str, str], timeout: float) -> Response:
        seen.append(headers)
        return Response(url=url, status=200)

    c = HttpClient(user_agent="t", min_host_interval=0, transport=transport)
    c.get("https://a.example/x")
    c.get("https://a.example/y", accept="text/html")
    assert seen[0]["Accept"] == "application/json" and seen[1]["Accept"] == "text/html"
