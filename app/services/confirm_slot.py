"""
Confirming a slot: the dispatcher has picked one of the suggestions from "find best slot" and confirms it.

The browser only says WHICH suggestion it saw (technician, day, arrival window, and the stops on either side of the
new job). Nothing it sends is trusted as a time: the server searches that technician's day again, under the same
write lock that saves the booking, and books only if the same option is still there. So two dispatchers cannot book
the same hole, and a route that changed while someone was looking (a sync, another booking, time passing) gives a
clear "no longer available" instead of a silently wrong booking. The planned arrival saved is the one just recomputed.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from ..domain.timeutil import parse_date
from . import bookings
from .dispatch_view import compute_slots
from .settings_store import get_settings

MAX_DAYS_AHEAD = 13          # the slot finder looks at most 14 days ahead (today + 13)


class BookingConflict(Exception):
    """The job or the slot is no longer what the dispatcher saw (the API answers 409)."""


def _int(sel: dict, key: str, lo: int, hi: int) -> int:
    v = sel.get(key)
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise ValueError(f"{key} must be a whole number from {lo} to {hi}")
    return v


def _stop_id(sel: dict, key: str) -> Optional[str]:
    v = sel.get(key)
    if v is None:
        return None
    if not isinstance(v, str) or not 0 < len(v) <= 80:
        raise ValueError(f"{key} must be a job id or empty")
    return v


def confirm_slot(conn, job_id: str, sel: dict, now: datetime, user_id: Optional[int]) -> Optional[dict]:
    """Book the selected slot. Returns the booking, None if the job does not exist; ValueError for a malformed
    request, BookingConflict when the job or slot has changed. Does its own locking (see below)."""
    # One writer at a time from here on: the check and the save must not interleave with another booking or a sync
    # write. No network call happens in here, so the lock is only held for a few milliseconds.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")

    job = conn.execute("SELECT work_status, active FROM jobs WHERE hcp_job_id = ?", (job_id,)).fetchone()
    if job is None:
        return None

    tech_id = sel.get("tech_id")
    if not isinstance(tech_id, str) or not 0 < len(tech_id) <= 80:
        raise ValueError("tech_id is required")
    day = parse_date(sel.get("date")) if isinstance(sel.get("date"), str) else None
    if day is None:
        raise ValueError("date must look like 2026-10-01")
    w_start = _int(sel, "window_start_min", 0, 24 * 60)
    w_end = _int(sel, "window_end_min", 1, 24 * 60)
    if w_end <= w_start:
        raise ValueError("The arrival window must end after it starts")
    w_len = sel.get("window_minutes")
    if w_len is not None and (not isinstance(w_len, int) or isinstance(w_len, bool) or not 15 <= w_len <= 720):
        raise ValueError("window_minutes must be a whole number from 15 to 720")
    after, before = _stop_id(sel, "after_stop_id"), _stop_id(sel, "before_stop_id")
    note = sel.get("note")
    if note is not None and not isinstance(note, str):
        raise ValueError("note must be text")
    note = " ".join((note or "").split())
    if len(note) > bookings.NOTE_MAX:
        raise ValueError(f"The note must be {bookings.NOTE_MAX} characters or fewer")

    settings = get_settings(conn)
    tz = ZoneInfo(settings["timezone"])
    today = now.astimezone(tz).date()
    if not today <= day <= today + timedelta(days=MAX_DAYS_AHEAD):
        raise ValueError(f"Pick a day from today to {MAX_DAYS_AHEAD} days ahead")

    if not job["active"] or job["work_status"] != "unscheduled":
        raise BookingConflict("Housecall Pro no longer lists this job as unscheduled, so there is nothing to book. "
                              "Refresh the page.")
    existing = bookings.get(conn, job_id, now)
    if existing is not None:
        raise BookingConflict(f"This job is already booked with {existing['tech_name'] or existing['tech_id']}; "
                              "remove that booking first to book it again.")

    res = compute_slots(conn, job_id, settings, now, window_minutes=w_len, days=[day], limit=10_000)
    option = next((o for o in res["options"] if o["tech_id"] == tech_id and o["date"] == day.isoformat()), None)
    if (option is None or (option["window_start_min"], option["window_end_min"]) != (w_start, w_end)
            or option["after_stop_id"] != after or option["before_stop_id"] != before):
        raise BookingConflict("That slot is no longer available: the route or the time has changed since it was "
                              "suggested. Find best slot again.")

    try:
        bookings.save(conn, job_id, tech_id, option, res["duration_min"], note, user_id, now)
    except sqlite3.IntegrityError:               # cannot happen under the lock; never leave a half-saved booking
        raise BookingConflict("Someone just booked this job. Refresh the page.")
    row = bookings.get(conn, job_id, now)
    return {"booking": bookings.public(row, tz)}
