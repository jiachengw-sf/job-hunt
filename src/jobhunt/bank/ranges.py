"""Choosing the option that contains a single value: "3.91 - 4.0" for a GPA of 4.0, "January
2028 - July 2028" for a graduation in 2028-05, "5+ years", "Below 3.0", "$50k - $75k".

An option equal to the value wins. Otherwise the value must fall inside exactly one option; when
two options overlap at the value (a boundary listed twice) or none contains it, nothing is
chosen and the field goes back to the user.
"""

from __future__ import annotations

import re
from collections.abc import Callable

MONTHS = {
    m: i + 1
    for i, names in enumerate(
        ("january jan", "february feb", "march mar", "april apr", "may", "june jun",
         "july jul", "august aug", "september sep sept", "october oct", "november nov",
         "december dec")
    )
    for m in names.split()
}  # fmt: skip

_NUM = r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([km])?\b"
_DASH = r"\s*(?:-|–|—|to|through|and)\s*"
_BELOW = re.compile(r"\b(?:below|under|less than|fewer than|lower than|before)\b|<(?!=)")
_AT_MOST = re.compile(r"\bup to\b|\bor (?:less|fewer|below|lower|under|earlier|before)\b|<=|≤")
_ABOVE = re.compile(r"\b(?:above|over|more than|greater than|higher than|after)\b|>(?!=)")
_AT_LEAST = re.compile(
    r"\bat least\b|\bor (?:more|greater|above|higher|over|later|after)\b|\+|>=|≥|\bminimum\b"
)
_YM = re.compile(r"^(\d{4})-(\d{2})(?:-\d{2})?$")
_MONTH_YEAR = re.compile(r"\b([a-z]{3,9})\.?,?\s+(\d{4})\b|\b(\d{1,2})/(\d{4})\b")
_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
# a season or quarter doesn't say which months it covers: never matched by year alone
_VAGUE = re.compile(r"\b(spring|summer|fall|autumn|winter|q[1-4]|quarter|semester|term)\b")

Bounds = tuple[float | None, bool, float | None, bool]  # low, low inclusive, high, high incl.


def _number(raw: str, suffix: str | None) -> float:
    n = float(raw.replace(",", ""))
    return n * {"k": 1_000, "m": 1_000_000}.get((suffix or "").lower(), 1)


def parse_number(value: str) -> float | None:
    """The one number a value states ("4.0", "$120,000", "120k"), else None."""
    found = re.findall(_NUM, value.casefold())
    return _number(*found[0]) if len(found) == 1 else None


def number_bounds(option: str) -> Bounds | None:
    """What numbers an option covers, or None if it names no number."""
    o = option.casefold()
    m = re.search(_NUM + _DASH + _NUM, o)
    if m:
        lo, hi = _number(m.group(1), m.group(2)), _number(m.group(3), m.group(4))
        if m.group(2) and not m.group(4):  # "$50k - 75k" reads the same as "$50k - $75k"
            hi = _number(m.group(3), m.group(2))
        return (min(lo, hi), True, max(lo, hi), True)
    found = re.findall(_NUM, o)
    if len(found) != 1:
        return None
    n = _number(*found[0])
    return _open_bounds(o, n)


def _open_bounds(o: str, n: float) -> Bounds:
    if _AT_MOST.search(o):
        return (None, False, n, True)
    if _AT_LEAST.search(o):
        return (n, True, None, False)
    if _BELOW.search(o):
        return (None, False, n, False)
    if _ABOVE.search(o):
        return (n, False, None, False)
    return (n, True, n, True)


def _inside(v: float, b: Bounds) -> bool:
    lo, lo_in, hi, hi_in = b
    if lo is not None and (v < lo or (v == lo and not lo_in)):
        return False
    return not (hi is not None and (v > hi or (v == hi and not hi_in)))


def _months(o: str) -> list[tuple[int, int]]:
    """Every month-year an option names, in order, as (year, month)."""
    out = []
    for m in _MONTH_YEAR.finditer(o):
        if m.group(1):
            if m.group(1) in MONTHS:
                out.append((int(m.group(2)), MONTHS[m.group(1)]))
        elif 1 <= int(m.group(3)) <= 12:
            out.append((int(m.group(4)), int(m.group(3))))
    return out


def date_bounds(option: str) -> Bounds | None:
    """The months an option covers ("Jan 2028 - Jul 2028", "2028", "Before 2026"), as
    year * 12 + month, or None if it names no date."""
    o = option.casefold()
    points = [y * 12 + mo for y, mo in _months(o)]
    if not points:
        if _VAGUE.search(o):
            return None
        years = [int(y) for y in _YEAR.findall(o)]
        if not years:
            return None
        if len(years) >= 2:
            return (years[0] * 12 + 1, True, years[1] * 12 + 12, True)
        lo, hi = years[0] * 12 + 1, years[0] * 12 + 12
        b = _open_bounds(o, 0)
        if b[0] is None and b[2] is not None:  # "before 2026" / "2026 or earlier"
            return (None, False, hi if b[3] else lo, b[3])
        if b[2] is None and b[0] is not None:  # "after 2030" / "2030 or later"
            return (lo if b[1] else hi, b[1], None, False)
        return (lo, True, hi, True)
    if len(points) >= 2:
        return (min(points[:2]), True, max(points[:2]), True)
    return _open_bounds(o, points[0])


def _pick(value: float, options: list[str], bounds: Callable[[str], Bounds | None]) -> str | None:
    hits = [o for o in options if (b := bounds(o)) is not None and _inside(value, b)]
    return hits[0] if len(hits) == 1 else None


def _same(value: str, options: list[str]) -> str | None:
    want = " ".join(value.casefold().split())
    return next((o for o in options if " ".join(o.casefold().split()) == want), None)


def pick_number(value: str, options: list[str]) -> str | None:
    """The option equal to the value, else the one numeric range that contains it."""
    if hit := _same(value, options):
        return hit
    v = parse_number(value)
    return None if v is None else _pick(v, options, number_bounds)


def pick_date(value: str, options: list[str]) -> str | None:
    """For a "YYYY-MM" or "YYYY-MM-DD" value: the option equal to it, else the one date range
    (by month) that contains it."""
    if hit := _same(value, options):
        return hit
    m = _YM.match(value.strip())
    if not m:
        return None
    return _pick(int(m.group(1)) * 12 + int(m.group(2)), options, date_bounds)
