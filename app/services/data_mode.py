"""
Keeps demo data and real Housecall Pro data apart.

The built-in demo (HCP_MODE=mock) uses ids that start with ``job_demo_`` / ``emp_demo_``; real Housecall Pro ids never
do. That is the only thing these rules look at, so a real row can never be mistaken for a demo row.

* Live mode (HCP_MODE=live) removes every demo row - jobs, their warranty details, notes and bookings, demo
  technicians - plus the caches built from the fake addresses, so nothing fake is ever shown next to real customers.
  It runs at startup and at the start of every sync, and it only ever deletes demo rows.
* Demo mode refuses to load demo data into a database that already holds real rows, so flipping ``.env`` back to
  mock by mistake cannot put fake jobs into the real schedule. Use another DATABASE_PATH for demos.

Users, settings and technician/shift configuration for real people are never touched.
"""

from __future__ import annotations

from typing import Dict

DEMO_JOB_PREFIX = "job_demo_"
DEMO_TECH_PREFIX = "emp_demo_"


def _like(prefix: str) -> str:
    return prefix.replace("_", "\\_") + "%"


_DEMO_JOBS = "hcp_job_id LIKE ? ESCAPE '\\'"
_DEMO_TECHS = "hcp_employee_id LIKE ? ESCAPE '\\'"


def count_demo(conn) -> Dict[str, int]:
    return {"jobs": conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {_DEMO_JOBS}", (_like(DEMO_JOB_PREFIX),)).fetchone()[0],
            "technicians": conn.execute(f"SELECT COUNT(*) FROM technicians WHERE {_DEMO_TECHS}",
                                        (_like(DEMO_TECH_PREFIX),)).fetchone()[0]}


def count_real(conn) -> Dict[str, int]:
    return {"jobs": conn.execute(f"SELECT COUNT(*) FROM jobs WHERE NOT ({_DEMO_JOBS})", (_like(DEMO_JOB_PREFIX),)).fetchone()[0],
            "technicians": conn.execute(f"SELECT COUNT(*) FROM technicians WHERE NOT ({_DEMO_TECHS})",
                                        (_like(DEMO_TECH_PREFIX),)).fetchone()[0]}


def has_real_data(conn) -> bool:
    return any(count_real(conn).values())


def purge_demo_data(conn) -> Dict[str, int]:
    """Delete every demo row (and the caches built from fake addresses). Returns what was removed; all zero when
    there was nothing to remove, in which case nothing at all is touched."""
    found = count_demo(conn)
    if not any(found.values()):
        return {"jobs": 0, "technicians": 0}
    jobs = _like(DEMO_JOB_PREFIX)
    conn.execute(f"DELETE FROM schedule_actions WHERE {_DEMO_JOBS}", (jobs,))
    conn.execute(f"DELETE FROM jobs WHERE {_DEMO_JOBS}", (jobs,))          # warranty details, notes and bookings cascade
    conn.execute(f"DELETE FROM technicians WHERE {_DEMO_TECHS}", (_like(DEMO_TECH_PREFIX),))
    # caches of the fake addresses and the roads between them, and the log of mock syncs
    conn.execute("DELETE FROM geocode_cache")
    conn.execute("DELETE FROM route_cache")
    conn.execute("DELETE FROM sync_runs WHERE mode = 'mock'")
    return found
