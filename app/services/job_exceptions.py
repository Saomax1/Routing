"""
"Deadline waived" notes.

The deadline rules (Admin > Settings) are targets, not hard limits: a customer may not be available, or there may
be another good reason to book a job later. A dispatcher records that here so the job stops showing as overdue,
stops earning deadline points, and the slot finder stops penalising slots that fall after the deadline.

This is local to the app (never written to Housecall Pro) and survives syncs.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..db import utcnow_iso

REASONS: Dict[str, str] = {
    "customer_unavailable": "Customer not available",
    "customer_requested": "Customer asked for a later date",
    "waiting_parts_auth": "Waiting on parts or authorization",
    "no_capacity": "No technician availability",
    "other": "Other (add a note)",
}
NOTE_MAX = 300


def reason_options() -> List[dict]:
    return [{"code": c, "label": l} for c, l in REASONS.items()]


_SELECT = ("SELECT e.hcp_job_id, e.reason, e.note, e.set_at, COALESCE(NULLIF(u.name, ''), u.email) AS set_by "
           "FROM job_exceptions e LEFT JOIN users u ON u.id = e.set_by")


def _public(row) -> dict:
    return {"reason": row["reason"], "reason_label": REASONS.get(row["reason"], row["reason"]),
            "note": row["note"], "set_by": row["set_by"], "set_at": row["set_at"]}


def exception_map(conn) -> Dict[str, dict]:
    return {r["hcp_job_id"]: _public(r) for r in conn.execute(_SELECT)}


def set_exception(conn, job_id: str, reason, note, user_id: Optional[int]) -> Optional[dict]:
    """Create or replace the note. Returns None if the job does not exist; ValueError for bad input."""
    job = conn.execute("SELECT work_status FROM jobs WHERE hcp_job_id = ?", (job_id,)).fetchone()
    if not job:
        return None
    if job["work_status"] != "unscheduled":
        raise ValueError("Only unscheduled jobs can have their deadline waived")
    if not isinstance(reason, str) or reason not in REASONS:
        raise ValueError("Choose a reason: " + ", ".join(REASONS.values()))
    if note is not None and not isinstance(note, str):
        raise ValueError("note must be text")
    note = " ".join((note or "").split())
    if len(note) > NOTE_MAX:
        raise ValueError(f"Note must be {NOTE_MAX} characters or fewer")
    if reason == "other" and not note:
        raise ValueError("Add a short note explaining the reason")
    conn.execute("INSERT INTO job_exceptions(hcp_job_id, reason, note, set_by, set_at) VALUES (?,?,?,?,?) "
                 "ON CONFLICT(hcp_job_id) DO UPDATE SET reason = excluded.reason, note = excluded.note, "
                 "set_by = excluded.set_by, set_at = excluded.set_at",
                 (job_id, reason, note, user_id, utcnow_iso()))
    return get_exception(conn, job_id)


def get_exception(conn, job_id: str) -> Optional[dict]:
    row = conn.execute(_SELECT + " WHERE e.hcp_job_id = ?", (job_id,)).fetchone()
    return _public(row) if row else None


def clear_exception(conn, job_id: str) -> bool:
    return conn.execute("DELETE FROM job_exceptions WHERE hcp_job_id = ?", (job_id,)).rowcount > 0
