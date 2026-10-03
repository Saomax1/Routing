"""
Turn raw Housecall Pro API objects into the flat records this app stores.

!!  The HCP field names below are UNVERIFIED best guesses (the public docs are rendered client-side
!!  and could not be read when this was written). Every lookup is tolerant: it tries several
!!  likely names and falls back to empty values rather than crashing. Run
!!  ``python scripts/phase0_probe.py`` against your real account; it prints which keys exist and
!!  which one holds the warranty text. If something maps wrong, this is the ONE file to adjust.
"""

from __future__ import annotations

import math
import re
from typing import Any, Optional

from ..domain.timeutil import parse_iso, to_iso
from ..domain.warranty_parser import keyword_present, looks_like_warranty


def pick(d: Any, *paths: str, default=None):
    """First non-empty value among dotted paths, e.g. pick(raw, 'schedule.scheduled_start', 'scheduled_start')."""
    for path in paths:
        cur = d
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                cur = None
                break
        if cur not in (None, "", [], {}):
            return cur
    return default


def _coordinates(lat: Any, lng: Any):
    """(lat, lng) as floats, or (None, None) when HCP gave nothing usable. 0, 0 is how a missing location is often
    stored; taking it at face value would pin the job in the Gulf of Guinea instead of geocoding its address."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None, None
    if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
        return None, None
    if lat == 0 and lng == 0:
        return None, None
    return lat, lng


def _name_of(v: Any) -> str:
    if isinstance(v, dict):
        return str(v.get("name") or v.get("title") or v.get("label") or "").strip()
    return str(v or "").strip()


def _full_name(d: Any) -> str:
    if not isinstance(d, dict):
        return str(d or "").strip()
    n = " ".join(str(x).strip() for x in (d.get("first_name"), d.get("last_name")) if x)
    return n or str(d.get("name") or d.get("company") or "").strip()


def canonical_work_status(raw_status: Any) -> str:
    s = str(raw_status or "").strip().lower().replace("_", " ").replace("-", " ")
    if not s:
        return "unknown"
    if "unschedul" in s or s in ("needs scheduling", "to be scheduled", "new"):
        return "unscheduled"
    if s == "scheduled":
        return "scheduled"
    if "progress" in s or "dispatch" in s or "on my way" in s:
        return "in_progress"
    if "complete" in s:
        return "complete"
    if "cancel" in s:
        return "canceled"
    return s.replace(" ", "_")


def canonical_trade(value: str, aliases: dict) -> str:
    v = (value or "").strip().upper()
    if not v:
        return ""
    if v in aliases:
        return aliases[v]
    for alias, trade in aliases.items():        # "Plumbing - Repair" -> PLB
        if len(alias) > 3 and alias in v:
            return trade
    return ""


_TEXT_TRADE_HINTS = [
    ("PLB", ["water heater", "toilet", "faucet", "drain", "disposal", "pipe", "plumb", "sewer", "stoppage", "leak"]),
    ("HVAC", ["hvac", "a/c", "ac unit", "air condition", "furnace", "thermostat", "cooling", "heat pump", "condenser", "no ac"]),
    ("ELEC", ["outlet", "breaker", "electrical", "wiring", "panel"]),
]


def guess_trade_from_text(text: str) -> str:
    t = text or ""
    for trade, words in _TEXT_TRADE_HINTS:
        if any(keyword_present(w, t) for w in words):
            return trade
    return ""


# Where a job's private notes may be: a list of {content|text|note|body}, a list of strings, or one string.
NOTE_KEYS = ("notes", "private_notes", "internal_notes", "job_notes")
_NOTE_TEXT_KEYS = ("content", "text", "note", "body")


def note_texts(raw: dict) -> list:
    """Every non-empty note on a job, as plain strings (any of the shapes above)."""
    out = []
    for key in NOTE_KEYS:
        value = raw.get(key)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict):
                item = next((item[k] for k in _NOTE_TEXT_KEYS if isinstance(item.get(k), str) and item[k].strip()), None)
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
    return out


def warranty_notes(raw: dict) -> list:
    """The notes that are warranty dispatch text (the only notes the app keeps)."""
    return [t for t in note_texts(raw) if looks_like_warranty(t)]


def normalize_job(raw: dict) -> dict:
    """Flat dict with the same keys as the ``jobs`` table (minus derived fields)."""
    addr = raw.get("address") if isinstance(raw.get("address"), dict) else {}
    cust = raw.get("customer") if isinstance(raw.get("customer"), dict) else {}

    description = str(pick(raw, "description", "job_description", "summary", default="") or "")
    # Dispatch text from warranty companies is often pasted into the job's PRIVATE NOTES rather than its description.
    # Only notes that look like a warranty dispatch are used; every other note is dropped here, before anything is
    # stored, shown or logged (a dispatcher's gate codes and remarks are none of this app's business).
    for text in warranty_notes(raw):
        if text not in description:
            description = f"{description}\n\n{text}" if description else text

    assigned = raw.get("assigned_employees") or raw.get("employees") or []
    assigned_ids = []
    for a in assigned if isinstance(assigned, list) else []:
        aid = a.get("id") if isinstance(a, dict) else a
        if aid:
            assigned_ids.append(str(aid))
    if not assigned_ids and isinstance(raw.get("assigned_employee_ids"), list):
        assigned_ids = [str(x) for x in raw["assigned_employee_ids"]]

    tags = []
    for t in raw.get("tags") or []:
        n = _name_of(t)
        if n:
            tags.append(n)

    lat, lng = _coordinates(pick(addr, "latitude", "lat"), pick(addr, "longitude", "lng", "lon"))

    # arrival_window = minutes after scheduled_start in which the technician may arrive (the promise to the customer)
    window = pick(raw, "schedule.arrival_window", "schedule.arrival_window_minutes", "arrival_window")
    try:
        window = int(window) if window is not None else None
    except (TypeError, ValueError):
        window = None

    return {
        "hcp_job_id": str(raw.get("id") or ""),
        "work_status": canonical_work_status(pick(raw, "work_status", "status")),
        "scheduled_start": to_iso(parse_iso(pick(raw, "schedule.scheduled_start", "scheduled_start"))),
        "scheduled_end": to_iso(parse_iso(pick(raw, "schedule.scheduled_end", "scheduled_end"))),
        "arrival_window_minutes": window,
        "completed_at": to_iso(parse_iso(pick(raw, "work_timestamps.completed_at", "completed_at"))),
        "assigned_employee_ids": assigned_ids,
        "customer_name": _full_name(cust),
        "customer_phone": str(pick(cust, "mobile_number", "home_number", "work_number", "phone", default="")),
        "street": str(addr.get("street") or "").strip(),
        "city": str(addr.get("city") or "").strip(),
        "state": str(addr.get("state") or "").strip(),
        "zip": str(addr.get("zip") or addr.get("postal_code") or "").strip(),
        "hcp_lat": lat, "hcp_lng": lng,
        "lead_source": _name_of(raw.get("lead_source")),
        "job_type": _name_of(pick(raw, "job_fields.job_type", "job_type", "type")),
        "tags": tags,
        "description_raw": str(description or ""),
        "hcp_created_at": to_iso(parse_iso(raw.get("created_at"))),
        "hcp_updated_at": to_iso(parse_iso(raw.get("updated_at"))),
    }


def normalize_employee(raw: dict) -> dict:
    name = _full_name(raw) or str(raw.get("email") or raw.get("id") or "Unknown")
    active = raw.get("active")
    if active is None:
        active = not raw.get("deleted", False)
    return {"hcp_employee_id": str(raw.get("id") or ""), "name": name, "active": bool(active),
            "role": str(raw.get("role") or "")}
