"""Default "where to check status" per ATS, from ``defaults/status_portals.yml``.

A URL is either fixed or derived from the posting's own URL by a named rule; nothing is guessed.
An ATS that isn't listed (or a rule that can't derive a URL) falls back to email.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from typing import Any
from urllib.parse import urlsplit

import yaml

from jobhunt.paths import package_file


@dataclass(frozen=True)
class StatusCheck:
    method: str  # portal | email | none
    url: str | None
    note: str


@cache
def registry() -> dict[str, Any]:
    text = package_file("defaults", "status_portals.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")


def workday_user_home(posting_url: str | None) -> str | None:
    """``https://<tenant>.wdN.myworkdayjobs.com/[<locale>/]<site>/userHome`` from a posting URL on
    that site, or None when the URL isn't one."""
    if not posting_url:
        return None
    u = urlsplit(posting_url)
    if not (u.scheme == "https" and u.hostname and u.hostname.endswith(".myworkdayjobs.com")):
        return None
    parts = [p for p in u.path.split("/") if p]
    if parts and _LOCALE.match(parts[0]):
        parts = parts[1:]
    if not parts or parts[0] in ("wday", "job", "login", "userHome"):
        return None
    return f"https://{u.hostname}/{parts[0]}/userHome"


RULES = {"workday_user_home": workday_user_home}


def status_check_defaults(ats: str | None, posting_url: str | None) -> StatusCheck:
    reg = registry()
    entry = (reg.get("portals") or {}).get((ats or "").lower())
    if entry is None:
        d = reg["default"]
        return StatusCheck(d["method"], None, d["note"].strip())
    url = entry.get("url")
    if rule := entry.get("url_rule"):
        url = RULES[rule](posting_url)
        if url is None:
            return StatusCheck("email", None, entry["fallback_note"].strip())
    return StatusCheck(entry["method"], url, entry["note"].strip())
