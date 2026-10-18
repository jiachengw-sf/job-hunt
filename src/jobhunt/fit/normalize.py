"""Normalizers shared by gates and preference scoring: location, work mode, employment type,
seniority, terms and pay. Anything that can't be read with confidence returns None (unknown),
never a guess."""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Any

# --- location -------------------------------------------------------------------------------

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT",
    "VA", "WA", "WV", "WI", "WY", "DC", "PR",
}  # fmt: skip
CA_PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
COUNTRY_NAMES = {
    "united states": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US", "us": "US",
    "america": "US", "canada": "CA", "united kingdom": "GB", "uk": "GB", "england": "GB",
    "scotland": "GB", "ireland": "IE", "germany": "DE", "france": "FR", "netherlands": "NL",
    "spain": "ES", "portugal": "PT", "italy": "IT", "poland": "PL", "sweden": "SE",
    "switzerland": "CH", "india": "IN", "singapore": "SG", "japan": "JP", "australia": "AU",
    "mexico": "MX", "brazil": "BR", "israel": "IL", "china": "CN", "hong kong": "HK",
    "south korea": "KR", "korea": "KR", "taiwan": "TW", "philippines": "PH", "remote us": "US",
}  # fmt: skip
CITY_COUNTRY = {
    "london": "GB", "toronto": "CA", "vancouver": "CA", "montreal": "CA", "waterloo": "CA",
    "ottawa": "CA", "calgary": "CA", "dublin": "IE", "berlin": "DE", "munich": "DE",
    "paris": "FR", "amsterdam": "NL", "bangalore": "IN", "bengaluru": "IN", "hyderabad": "IN",
    "singapore": "SG", "tokyo": "JP", "sydney": "AU", "tel aviv": "IL", "zurich": "CH",
    "new york city": "US", "nyc": "US", "san francisco": "US", "seattle": "US",
}  # fmt: skip

# Approximate metro centers (lat, lon) for radius checks. A city not listed is "unknown".
METROS: dict[str, tuple[float, float]] = {
    "new york": (40.71, -74.01), "brooklyn": (40.68, -73.94), "jersey city": (40.73, -74.08),
    "boston": (42.36, -71.06), "cambridge": (42.37, -71.11), "somerville": (42.39, -71.10),
    "philadelphia": (39.95, -75.17), "washington": (38.91, -77.04), "arlington": (38.88, -77.10),
    "baltimore": (39.29, -76.61), "pittsburgh": (40.44, -79.99), "chicago": (41.88, -87.63),
    "evanston": (42.05, -87.69), "detroit": (42.33, -83.05), "ann arbor": (42.28, -83.74),
    "columbus": (39.96, -83.00), "cleveland": (41.50, -81.69), "minneapolis": (44.98, -93.27),
    "st. louis": (38.63, -90.20), "kansas city": (39.10, -94.58), "atlanta": (33.75, -84.39),
    "miami": (25.76, -80.19), "tampa": (27.95, -82.46), "orlando": (28.54, -81.38),
    "charlotte": (35.23, -80.84), "raleigh": (35.78, -78.64), "durham": (35.99, -78.90),
    "nashville": (36.16, -86.78), "memphis": (35.15, -90.05), "austin": (30.27, -97.74),
    "dallas": (32.78, -96.80), "houston": (29.76, -95.37), "san antonio": (29.42, -98.49),
    "denver": (39.74, -104.99), "boulder": (40.01, -105.27), "phoenix": (33.45, -112.07),
    "salt lake city": (40.76, -111.89), "las vegas": (36.17, -115.14),
    "los angeles": (34.05, -118.24), "santa monica": (34.02, -118.49),
    "irvine": (33.68, -117.83), "san diego": (32.72, -117.16),
    "san francisco": (37.77, -122.42), "oakland": (37.80, -122.27), "berkeley": (37.87, -122.27),
    "palo alto": (37.44, -122.14), "mountain view": (37.39, -122.08),
    "menlo park": (37.45, -122.18), "sunnyvale": (37.37, -122.04), "santa clara": (37.35, -121.96),
    "san jose": (37.34, -121.89), "redwood city": (37.49, -122.24), "seattle": (47.61, -122.33),
    "bellevue": (47.61, -122.20), "redmond": (47.67, -122.12), "kirkland": (47.68, -122.21),
    "portland": (45.52, -122.68), "wilmington": (39.74, -75.55), "newark": (40.74, -74.17),
    "toronto": (43.65, -79.38), "waterloo": (43.46, -80.52), "montreal": (45.50, -73.57),
    "vancouver": (49.28, -123.12), "ottawa": (45.42, -75.70), "london": (51.51, -0.13),
}  # fmt: skip

_REMOTE_RE = re.compile(r"\bremote\b|\bwork from home\b|\bwfh\b|\banywhere\b", re.I)


def location_country(loc: str) -> str | None:
    """ISO alpha-2 for one location string, or None when it can't be read."""
    s = loc.strip()
    low = re.sub(r"[()\-–—/]", " ", s.lower())
    low = re.sub(r"\s+", " ", low).strip()
    parts = [p.strip() for p in re.split(r"[,;]", s) if p.strip()]
    if parts:
        last = parts[-1].strip().upper().rstrip(".")
        if last in US_STATES and len(parts) >= 2:
            return "US"
        if last in CA_PROVINCES and len(parts) >= 2:
            return "CA"
    for name, code in sorted(COUNTRY_NAMES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", low):
            if name == "us" and not re.search(r"\b(remote|us|u\.s\.)\b", low):
                continue
            return code
    city = city_name(s)
    if city and city in CITY_COUNTRY:
        return CITY_COUNTRY[city]
    return None


def city_name(loc: str) -> str | None:
    first = re.split(r"[,;(]", loc)[0].strip().lower()
    first = re.sub(r"^(remote|hybrid|on-?site)\s*[-–:]\s*", "", first)
    first = first.replace("new york city", "new york").replace("nyc", "new york")
    return first or None


def haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def distance_miles(loc_a: str, loc_b: str) -> float | None:
    a, b = METROS.get(city_name(loc_a) or ""), METROS.get(city_name(loc_b) or "")
    if a is None or b is None:
        return None
    return haversine_miles(a, b)


def is_remote_location(loc: str) -> bool:
    return bool(_REMOTE_RE.search(loc))


# --- work mode ------------------------------------------------------------------------------

_MODE_TEXT = [
    (
        "remote",
        re.compile(
            r"\b(fully|100%|completely) remote\b|\bremote[- ](first|friendly|only)\b|"
            r"\bwork (from )?anywhere\b|\bthis (role|position) is remote\b",
            re.I,
        ),
    ),
    ("hybrid", re.compile(r"\bhybrid\b", re.I)),
    (
        "onsite",
        re.compile(
            r"\bon[- ]?site\b|\bin[- ]office\b|\bin[- ]person\b|"
            r"\b(\d|five|four|three) days (a|per) week in (the )?office\b",
            re.I,
        ),
    ),
]


def work_modes(job_mode: str | None, locations: list[str], title: str, text: str) -> set[str]:
    """Modes the job states. Empty set = unknown."""
    if job_mode:
        m = re.sub(r"[^a-z]", "", job_mode.lower())
        if m in ("remote", "hybrid"):
            return {m}
        if m in ("onsite", "inoffice", "inperson", "office"):
            return {"onsite"}
    modes: set[str] = set()
    if any(is_remote_location(loc) for loc in locations) or re.search(r"\bremote\b", title, re.I):
        modes.add("remote")
    if any(re.search(r"\bhybrid\b", loc, re.I) for loc in locations):
        modes.add("hybrid")
    if modes:
        return modes
    for mode, rx in _MODE_TEXT:
        if rx.search(text):
            modes.add(mode)
    return modes


# --- employment type and seniority ----------------------------------------------------------


def employment_type(raw: str | None, title: str) -> str | None:
    if re.search(r"\bco-?op\b", title, re.I):
        return "co-op"
    if re.search(r"\bintern(ship)?s?\b", title, re.I):
        return "internship"
    if not raw:
        return None
    r = re.sub(r"[^a-z]", "", raw.lower())
    if r.startswith("intern"):
        return "internship"
    if r in ("coop", "cooperative"):
        return "co-op"
    if r in ("fulltime", "permanent", "regular", "fulltimeemployee", "salaried"):
        return "full-time"
    if r in ("parttime",):
        return "part-time"
    if r in ("contract", "contractor", "temporary", "temp", "freelance", "contracttohire"):
        return "contract"
    return None


_SENIORITY = [
    ("internship", re.compile(r"\bintern(ship)?s?\b|\bco-?op\b|\bapprentice|\bsummer (analyst|associate)\b", re.I)),  # noqa: E501
    ("new_grad", re.compile(r"\bnew grad(uate)?s?\b|\buniversity grad(uate)?\b|\bearly[- ]career\b|"
                            r"\bgraduate (program|scheme|engineer|analyst)\b|\bcampus\b|\bclass of 20\d\d\b", re.I)),  # noqa: E501
    ("senior", re.compile(r"\b(senior|sr\.?|staff|principal|lead|architect|distinguished|director|"
                          r"head of|vp|vice president)\b|\b(engineer|developer|scientist) iii\b", re.I)),  # noqa: E501
    ("entry", re.compile(r"\b(junior|jr\.?|entry[- ]level|associate)\b|\b(engineer|analyst|developer|"  # noqa: E501
                         r"scientist) i\b|\blevel 1\b", re.I)),
    ("mid", re.compile(r"\b(engineer|analyst|developer|scientist) ii\b|\bmid[- ]level\b", re.I)),
]  # fmt: skip


def seniority(title: str) -> str | None:
    for level, rx in _SENIORITY:
        if rx.search(title):
            return level
    return None


SENIORITY_COMPATIBLE = {"new_grad": {"new_grad", "entry"}, "entry": {"entry", "new_grad"}}


def seniority_ok(job_level: str, wanted: list[str]) -> bool:
    allowed = set()
    for w in wanted:
        allowed |= SENIORITY_COMPATIBLE.get(w, {w})
    return job_level in allowed


# --- terms ----------------------------------------------------------------------------------

_TERM_RE = re.compile(r"\b(spring|summer|fall|autumn|winter)\s*[' ]?\s*(20\d\d|'?\d\d)\b", re.I)
SEASON_MONTHS = {"winter": (1, 4), "spring": (1, 5), "summer": (5, 8), "fall": (9, 12)}


def norm_term(raw: str) -> str | None:
    m = _TERM_RE.search(raw)
    if not m:
        return None
    season = m.group(1).lower().replace("autumn", "fall")
    yr = m.group(2).lstrip("'")
    year = int(yr) if len(yr) == 4 else 2000 + int(yr)
    return f"{season.capitalize()} {year}"


def job_terms(structured: list[str], title: str, text: str) -> list[str]:
    found = [t for t in (norm_term(s) for s in structured) if t]
    if not found:
        for m in _TERM_RE.finditer(title + "\n" + text[:1500]):
            t = norm_term(m.group(0))
            if t and t not in found:
                found.append(t)
    return found


def term_window(term: str) -> tuple[date, date] | None:
    m = re.match(r"(\w+) (\d{4})", term)
    if not m or m.group(1).lower() not in SEASON_MONTHS:
        return None
    a, b = SEASON_MONTHS[m.group(1).lower()]
    y = int(m.group(2))
    return date(y, a, 1), date(y, b, 28)


# --- pay ------------------------------------------------------------------------------------

HOURS_PER_YEAR = 2080


def annual_pay(pay: dict[str, Any] | None) -> tuple[float | None, float | None, str | None]:
    """(min, max, note) in annual terms. The unit comes from the source's interval; when the
    source gives none, it is read from the amount (under 500 = hourly) and the note says so."""
    if not pay or (pay.get("min") is None and pay.get("max") is None):
        return None, None, None
    lo, hi = pay.get("min"), pay.get("max")
    interval = f"{pay.get('interval') or ''} {pay.get('text') or ''}".lower()
    note = None
    if re.search(r"hour|wage", interval):
        mult = HOURS_PER_YEAR
    elif re.search(r"month", interval):
        mult = 12
    elif re.search(r"week", interval):
        mult = 52
    elif re.search(r"year|annual|salary", interval):
        mult = 1
    else:
        ref = hi if hi is not None else lo
        mult = HOURS_PER_YEAR if ref is not None and ref < 500 else 1
        note = "pay unit read from the amount (the posting doesn't say hourly or annual)"
    return (
        lo * mult if lo is not None else None,
        hi * mult if hi is not None else None,
        note,
    )


def to_annual(amount: float | None, unit: str) -> float | None:
    if amount is None:
        return None
    return amount * HOURS_PER_YEAR if unit == "hourly" else amount
