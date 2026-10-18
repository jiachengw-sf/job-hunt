"""HTTP access for public, documented job sources. Milestone 0 needs only a reachability probe;
Milestone 1 adds per-host throttling, conditional GETs, Retry-After and backoff here."""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass


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
