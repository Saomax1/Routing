"""
Confirmed bookings: a dispatcher picks one of the suggested slots and confirms it.

The app is read-only against Housecall Pro, so a booking is saved here and laid over the job: the job leaves the
unscheduled queue and shows up as a stop on that technician's route (and every later "find best slot" plans around it),
but Housecall Pro still has it unscheduled until someone enters it there. A booking is "in force" only while

  * its job is still active and UNSCHEDULED in Housecall Pro (once HCP shows it scheduled, HCP is the truth), and
  * its arrival window has not ended (a promise that was never entered in HCP must not hide the job from the queue).

Everything else (``drop_stale``) is housekeeping. The storage lives here; the flow that checks a slot before saving it
is in ``confirm_slot.py``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from ..domain.timeutil import minutes_of_day, parse_iso, to_iso

NOTE_MAX = 300

# A booking is in force only for a job that is still open and unscheduled in HCP, until its window has ended.
_IN_FORCE = ("FROM bookings b JOIN jobs j ON j.hcp_job_id = b.hcp_job_id AND j.active = 1 AND j.work_status = 'unscheduled' "
             "LEFT JOIN users u ON u.id = b.booked_by LEFT JOIN technicians t ON t.hcp_employee_id = b.tech_id "
             "WHERE b.window_end_at > ?")
_COLUMNS = ("SELECT b.*, COALESCE(NULLIF(u.name, ''), u.email) AS booked_by_name, t.name AS tech_name, "
            "t.color AS tech_color ")


def in_force(conn, now: datetime, job_id: Optional[str] = None, lo: Optional[str] = None,
             hi: Optional[str] = None) -> List[sqlite3.Row]:
    """Bookings in force, soonest first; optionally one job, or those arriving in [lo, hi) (UTC ISO strings)."""
    sql, args = _COLUMNS + _IN_FORCE, [to_iso(now)]
    if job_id is not None:
        sql, args = sql + " AND b.hcp_job_id = ?", args + [job_id]
    if lo is not None and hi is not None:
        sql, args = sql + " AND b.arrive_at >= ? AND b.arrive_at < ?", args + [lo, hi]
    return conn.execute(sql + " ORDER BY b.arrive_at, b.hcp_job_id", args).fetchall()


def get(conn, job_id: str, now: datetime) -> Optional[sqlite3.Row]:
    rows = in_force(conn, now, job_id=job_id)
    return rows[0] if rows else None


def public(row, tz: ZoneInfo) -> dict:
    """What the browser sees. Times are UTC ISO strings plus minutes-of-day in the company time zone."""
    arrive, end = parse_iso(row["arrive_at"]), parse_iso(row["end_at"])
    w_start, w_end = parse_iso(row["window_start_at"]), parse_iso(row["window_end_at"])
    ws = minutes_of_day(w_start, tz)
    return {
        "job_id": row["hcp_job_id"], "tech_id": row["tech_id"], "tech_name": row["tech_name"] or row["tech_id"],
        "tech_color": row["tech_color"], "date": arrive.astimezone(tz).date().isoformat(),
        "arrive_iso": row["arrive_at"], "end_iso": row["end_at"],
        "window_start_iso": row["window_start_at"], "window_end_iso": row["window_end_at"],
        "arrive_min": minutes_of_day(arrive, tz), "end_min": minutes_of_day(end, tz),
        "window_start_min": ws, "window_end_min": ws + int((w_end - w_start).total_seconds() // 60),
        "duration_min": row["duration_min"], "note": row["note"],
        "booked_by": row["booked_by_name"], "booked_at": row["booked_at"],
    }


def save(conn, job_id: str, tech_id: str, option: dict, duration_min: int, note: str,
         user_id: Optional[int], now: datetime) -> None:
    """Store a booking for ``option`` (a slot-finder option) and log it. The caller has already checked the slot."""
    conn.execute("DELETE FROM bookings WHERE hcp_job_id = ?", (job_id,))      # an expired one (no longer in force)
    conn.execute("INSERT INTO bookings(hcp_job_id, tech_id, arrive_at, end_at, window_start_at, window_end_at, "
                 "duration_min, note, booked_by, booked_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (job_id, tech_id, option["start_iso"], option["end_iso"], option["window_start_iso"],
                  option["window_end_iso"], int(duration_min), note, user_id, to_iso(now)))
    _log(conn, now, user_id, job_id, new_start=option["start_iso"], new_tech=tech_id,
         response="booked in this app (not sent to Housecall Pro)")


def remove(conn, job_id: str, now: datetime, user_id: Optional[int]) -> bool:
    """Take a booking away: the job goes back to the unscheduled queue. False if there was no booking in force."""
    row = get(conn, job_id, now)
    if row is None:
        return False
    conn.execute("DELETE FROM bookings WHERE hcp_job_id = ?", (job_id,))
    _log(conn, now, user_id, job_id, old_start=row["arrive_at"], old_tech=row["tech_id"],
         response="booking removed in this app")
    return True


def drop_stale(conn, now: datetime) -> int:
    """Housekeeping after a sync: forget bookings HCP has caught up with (the job is scheduled there now, so an old
    row must not come back to life if the job is ever unscheduled again) and bookings whose window has ended."""
    return conn.execute("DELETE FROM bookings WHERE window_end_at <= ? OR hcp_job_id IN "
                        "(SELECT hcp_job_id FROM jobs WHERE work_status != 'unscheduled')", (to_iso(now),)).rowcount


def _log(conn, now, user_id, job_id, old_start=None, old_tech=None, new_start=None, new_tech=None, response="") -> None:
    conn.execute("INSERT INTO schedule_actions(at, user_id, hcp_job_id, old_start, old_tech, new_start, new_tech, "
                 "success, response) VALUES (?,?,?,?,?,?,?,1,?)",
                 (to_iso(now), user_id, job_id, old_start, old_tech, new_start, new_tech, response))
