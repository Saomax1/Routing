"""Time helpers. Storage is UTC ISO-8601 ('...Z'); scheduling math happens in the company timezone."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo


def parse_iso(value) -> Optional[datetime]:
    """Parse an ISO-8601 string (with or without offset / 'Z') into an aware UTC datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hhmm_to_minutes(s: str, default: int = 480) -> int:
    try:
        h, m = str(s).split(":")[:2]
        return max(0, min(24 * 60, int(h) * 60 + int(m)))
    except (ValueError, AttributeError):
        return default


def minutes_to_hhmm(m: int) -> str:
    m = int(m)
    return f"{m // 60:02d}:{m % 60:02d}"


def local_date(dt: datetime, tz: ZoneInfo) -> date:
    return dt.astimezone(tz).date()


def minutes_of_day(dt: datetime, tz: ZoneInfo) -> int:
    l = dt.astimezone(tz)
    return l.hour * 60 + l.minute


def at_local_minutes(d: date, minutes: int, tz: ZoneInfo) -> datetime:
    """Aware datetime for ``minutes`` after local midnight on date ``d``."""
    base = datetime.combine(d, time(0, 0), tzinfo=tz)
    return (base + timedelta(minutes=int(minutes)))


def ceil_to(value: float, step: int) -> int:
    step = max(1, int(step))
    return int(-(-value // step) * step)


def parse_date(s: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None
