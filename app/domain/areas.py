"""
Service areas for the running totals on the Areas tab.

An "area" is just the city (or ZIP code) on the job's address, so it works even for jobs that could not be
geocoded. Pure functions only.
"""

from __future__ import annotations

import re
from typing import Tuple

GROUP_BY = ("city", "zip")
UNKNOWN_KEY, UNKNOWN_LABEL = "none", "No address"

_ZIP5 = re.compile(r"\d{5}")


def _tidy_city(city: str) -> str:
    """Collapse whitespace; title-case only when HCP gave us ALL CAPS / all lower (keeps 'McKinney' intact)."""
    c = " ".join(str(city or "").split())
    return c.title() if c.isupper() or c.islower() else c


def area_of(job: dict, group_by: str = "city") -> Tuple[str, str]:
    """(key, label) of the area a job belongs to. Keys are stable across syncs and case-insensitive."""
    city = _tidy_city(job.get("city"))
    m = _ZIP5.match(str(job.get("zip") or "").strip())
    zip5 = m.group(0) if m else ""
    city_area = (f"city:{city.casefold()}", city) if city else None
    zip_area = (f"zip:{zip5}", f"ZIP {zip5}") if zip5 else None
    ordered = (zip_area, city_area) if group_by == "zip" else (city_area, zip_area)
    for choice in ordered:
        if choice:
            return choice
    return UNKNOWN_KEY, UNKNOWN_LABEL
