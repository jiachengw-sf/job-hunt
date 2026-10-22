"""Times for people: stored as UTC ISO 8601, shown in the user's own time zone, and read from
natural local phrases ("today 3pm", "yesterday 9:30pm", "Oct 21 21:30", "2 days ago").

The zone is ``tracking.timezone`` (IANA) when set, else this computer's. Each instant is
converted on its own, so a date on the other side of a daylight-saving change shows its own
offset (PDT vs PST).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jobhunt.discovery.text import iso
from jobhunt.errors import ConfigError, ValidationFailed

_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}  # fmt: skip
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_PARTS = {
    "morning": time(9),
    "noon": time(12),
    "midday": time(12),
    "afternoon": time(15),
    "evening": time(19),
    "tonight": time(20),
    "night": time(21),
}


def zone(name: str | None) -> tzinfo | None:
    """The configured zone, or None for this computer's local time."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError(
            f"tracking.timezone {name!r} is not a known time zone.",
            next_actions=["Use an IANA name such as America/Los_Angeles, or null."],
        ) from e


def parse_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def to_local(ts: str, tz: tzinfo | None) -> datetime:
    dt = parse_utc(ts)
    return dt.astimezone(tz) if tz else dt.astimezone()


def _clock(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def fmt_date(ts: str | None, tz: tzinfo | None) -> str:
    """'Wed, Oct 21, 2026' (empty for no time)."""
    if not ts:
        return ""
    d = to_local(ts, tz)
    return f"{d:%a}, {d:%b} {d.day}, {d.year}"


def fmt_local(ts: str | None, tz: tzinfo | None, *, approx: bool = False) -> str:
    """'Wed, Oct 21, 2026 · 9:42 PM PDT'. An approximate time shows the date only."""
    if not ts:
        return ""
    if approx:
        return fmt_date(ts, tz) + " (time approximate)"
    d = to_local(ts, tz)
    return f"{fmt_date(ts, tz)} · {_clock(d)} {d.tzname() or ''}".rstrip()


def local_day(ts: str, tz: tzinfo | None) -> date:
    return to_local(ts, tz).date()


# --- reading what people type --------------------------------------------------------------

_TIME_RE = re.compile(
    r"(?:\bat\s+)?\b(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?)?(?=\s|$)", re.I
)


def _parse_time(text: str) -> tuple[time | None, str]:
    """(time found, the rest of the text)."""
    for word, t in _PARTS.items():
        m = re.search(rf"\b(?:this\s+|in\s+the\s+)?{word}\b", text)
        if m:
            return t, (text[: m.start()] + text[m.end() :]).strip()
    best = None
    for m in _TIME_RE.finditer(text):
        if m.group("m") or m.group("ap"):
            best = m  # a bare number ("21" in "Oct 21") is a day, not a time
    if not best:
        return None, text
    h, mi, ap = int(best.group("h")), int(best.group("m") or 0), best.group("ap")
    if ap:
        if not 1 <= h <= 12:
            raise ValidationFailed(f"{best.group(0).strip()!r} is not a time.")
        h = h % 12 + (12 if ap.lower().startswith("p") else 0)
    if h > 23 or mi > 59:
        raise ValidationFailed(f"{best.group(0).strip()!r} is not a time.")
    return time(h, mi), (text[: best.start()] + text[best.end() :]).strip()


def _parse_day(text: str, today: date) -> date | None:
    t = re.sub(r"[,.]", " ", text).strip()
    t = re.sub(r"\s+", " ", t)
    if t in ("", "today", "now"):
        return today
    if t == "yesterday":
        return today - timedelta(days=1)
    if m := re.fullmatch(r"(\d+) days? ago", t):
        return today - timedelta(days=int(m.group(1)))
    if (m := re.fullmatch(r"(?:last )?(\w+day)", t)) and m.group(1) in _WEEKDAYS:
        back = (today.weekday() - _WEEKDAYS.index(m.group(1))) % 7 or 7
        return today - timedelta(days=back)
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t):
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if m := re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", t):
        y = int(m.group(3)) if m.group(3) else None
        return _with_year(int(m.group(1)), int(m.group(2)), y, today)
    m = re.fullmatch(r"(?:\w+day )?([a-z]+) (\d{1,2})(?:st|nd|rd|th)?(?: (\d{4}))?", t)
    if m and m.group(1) in _MONTHS:
        y = int(m.group(3)) if m.group(3) else None
        return _with_year(_MONTHS[m.group(1)], int(m.group(2)), y, today)
    return None


def _with_year(month: int, day: int, year: int | None, today: date) -> date:
    if year is not None:
        return date(year + 2000 if year < 100 else year, month, day)
    d = date(today.year, month, day)
    return d if d <= today + timedelta(days=180) else date(today.year - 1, month, day)


def parse_when(
    text: str, now: str, tz: tzinfo | None, *, allow_future: bool = False
) -> tuple[str, bool]:
    """A local phrase as (UTC ISO, approximate). A date without a time is approximate (noon).

    Raises ValidationFailed for anything it can't read, so nothing is guessed.
    """
    raw = text.strip()
    now_dt = parse_utc(now)
    if raw.lower() == "now":
        return iso(now_dt), False
    try:  # full ISO with an offset or Z: an exact instant
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            return _check(dt, now_dt, raw, allow_future), False
    except ValueError:
        pass
    local_now = now_dt.astimezone(tz) if tz else now_dt.astimezone()
    low = re.sub(r"^(\d{4}-\d{2}-\d{2})t(\d)", r"\1 \2", raw.lower())  # 2026-10-21T21:30
    low = re.sub(r"(\d{1,2}:\d{2}):\d{2}\b", r"\1", low)  # seconds don't matter here
    try:
        t, rest = _parse_time(low)
        day = _parse_day(rest, local_now.date())
    except ValueError as e:
        raise ValidationFailed(f"Can't read the time {text!r}: {e}.") from e
    if day is None:
        raise ValidationFailed(
            f"Can't read the time {text!r}.",
            next_actions=[
                'Use e.g. "now", "today 3pm", "yesterday 21:30", "Oct 21 9:42pm", "2026-10-21 '
                '21:30" or "2 days ago".'
            ],
        )
    approx = t is None
    naive = datetime.combine(day, t or time(12))
    # in this computer's zone (no tz), astimezone() uses that day's own offset
    dt = naive.replace(tzinfo=tz) if tz else naive.astimezone()
    return _check(dt, now_dt, text, allow_future), approx


def _check(dt: datetime, now_dt: datetime, text: str, allow_future: bool) -> str:
    if not allow_future and dt > now_dt + timedelta(minutes=5):
        raise ValidationFailed(f"{text!r} is in the future.")
    return iso(dt)
