"""HTTP access for public, documented job sources.

Request hygiene lives here and nowhere else:
  - a minimum gap between requests to the same host (1 second by default)
  - conditional GETs (``If-None-Match`` / ``If-Modified-Since``); a 304 means "unchanged"
  - on 429 and 5xx, back off with jitter, honoring ``Retry-After``

The transport is injectable, so tests and offline fixtures never touch the network.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 120.0


@dataclass(frozen=True)
class Probe:
    url: str
    reachable: bool
    status: int | None
    detail: str


def probe(url: str, *, user_agent: str, timeout: float = 6.0) -> Probe:
    """Any HTTP response (even 404) means the host is reachable."""
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return Probe(url, True, resp.status, f"HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        return Probe(url, True, e.code, f"HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        return Probe(url, False, None, f"unreachable: {reason}")


# --- client ---------------------------------------------------------------------------------


@dataclass
class Response:
    url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)  # lower-cased names
    body: bytes = b""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class TransportError(Exception):
    """The host could not be reached (DNS, connection, timeout)."""


class Transport(Protocol):
    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> Response: ...


def urllib_transport(url: str, headers: dict[str, str], timeout: float) -> Response:
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _response(url, resp.status, resp.headers, resp.read())
    except urllib.error.HTTPError as e:
        return _response(url, e.code, e.headers, e.read() if e.fp else b"")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TransportError(f"{url} unreachable: {getattr(e, 'reason', e)}") from e


def _response(url: str, status: int, headers: Any, body: bytes) -> Response:
    hdrs = {k.lower(): v for k, v in (headers or {}).items()}
    if hdrs.get("content-encoding") == "gzip" and body:
        body = gzip.decompress(body)
    return Response(url=url, status=status, headers=hdrs, body=body)


class HttpClient:
    def __init__(
        self,
        *,
        user_agent: str,
        min_host_interval: float = 1.0,
        timeout: float = 20.0,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        transport: Transport = urllib_transport,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.user_agent = user_agent
        self.min_host_interval = min_host_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
        self.rng = rng
        self._last_request: dict[str, float] = {}
        self.requests = 0

    def get(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> Response:
        """GET with throttle, conditional headers and retries. Returns the final response (any
        status, including 304); raises TransportError if the host stays unreachable."""
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        attempt = 0
        while True:
            self._throttle(url)
            try:
                resp = self.transport(url, headers, self.timeout)
            except TransportError:
                if attempt >= self.max_retries:
                    raise
                self.sleep(self._backoff(attempt))
                attempt += 1
                continue
            if resp.status not in RETRY_STATUSES or attempt >= self.max_retries:
                return resp
            delay = retry_after_seconds(resp.headers.get("retry-after"))
            if delay is None:
                delay = self._backoff(attempt)
            elif delay > MAX_RETRY_AFTER_SECONDS:
                return resp  # the host asked for a long pause: give up for this run
            self.sleep(delay)
            attempt += 1

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with jitter: base * 2^attempt * [0.5, 1.5)."""
        return self.backoff_base * (2**attempt) * (0.5 + self.rng())

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        last = self._last_request.get(host)
        if last is not None:
            wait = self.min_host_interval - (self.clock() - last)
            if wait > 0:
                self.sleep(wait)
        self._last_request[host] = self.clock()
        self.requests += 1


def retry_after_seconds(value: str | None) -> float | None:
    """``Retry-After`` is either delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


# --- offline fixtures -----------------------------------------------------------------------


class FixtureTransport:
    """Serves ``<root>/<host>/<path>`` (``.json`` appended when the path has no suffix); the
    query string is ignored. Missing files are 404. ETags are content hashes, so conditional
    GETs behave as they do live."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[str] = []

    def path_for(self, url: str) -> Path:
        parts = urlsplit(url)
        rel = parts.path.strip("/")
        if not Path(rel).suffix:
            rel += ".json"
        return self.root / parts.netloc.lower() / rel

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> Response:
        self.calls.append(url)
        path = self.path_for(url)
        if not path.is_file():
            return Response(url=url, status=404, body=b'{"error": "not found"}')
        body = path.read_bytes()
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        if headers.get("If-None-Match") == etag:
            return Response(url=url, status=304, headers={"etag": etag})
        return Response(url=url, status=200, headers={"etag": etag}, body=body)
