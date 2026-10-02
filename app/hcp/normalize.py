"""
Turn raw Housecall Pro API objects into the flat records this app stores.

!!  The HCP field names below are UNVERIFIED best guesses (the public docs are rendered client-side
!!  and could not be read when this was written). Every lookup is tolerant: it tries several
!!  likely names and falls back to empty values rather than crashing. Run
!!  ``python scripts/phase0_probe.py`` against your real account; it prints which keys exist and
!!  which one holds the warranty text. If something maps wrong, this is the ONE file to adjust.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..domain.timeutil import parse_iso, to_iso
from ..domain.warranty_parser import keyword_present


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


_OTHER_WARRANTY_HINTS = ["warranty", "first american", "choice home", "hwa", "fidelity national", "old republic",
                         "2-10", "select home", "liberty home guard", "cinch", "frontdoor", "total home protection"]


def classify_source(lead_source: str, tags: list, warranty_company: Optional[str]) -> str:
    """ahs | other_warranty | direct"""
    if warranty_company:
        return "ahs" if warranty_company.upper() in ("AHS", "FRONTDOOR") else "other_warranty"
    hay = " ".join([lead_source or ""] + [str(t) for t in tags]).lower()
    if re.search(r"\bahs\b|american home shield|frontdoor", hay):
        return "ahs"
    if any(h in hay for h in _OTHER_WARRANTY_HINTS):
        return "other_warranty"
    return "direct"


def normalize_job(raw: dict) -> dict:
    """Flat dict with the same keys as the ``jobs`` table (minus derived fields)."""
    addr = raw.get("address") if isinstance(raw.get("address"), dict) else {}
    cust = raw.get("customer") if isinstance(raw.get("customer"), dict) else {}

    description = pick(raw, "description", "job_description", "summary", default="")
    if not description:
        notes = raw.get("notes")
        if isinstance(notes, list):
            description = "\n".join(str(n.get("content") or n.get("text") or "") if isinstance(n, dict) else str(n)
                                    for n in notes)
        elif isinstance(notes, str):
            description = notes

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

    lat = pick(addr, "latitude", "lat")
    lng = pick(addr, "longitude", "lng", "lon")
    try:
        lat = float(lat) if lat is not None else None
        lng = float(lng) if lng is not None else None
    except (TypeError, ValueError):
        lat = lng = None

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
