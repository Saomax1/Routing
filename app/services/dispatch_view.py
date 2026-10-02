"""
Read-side services: turn database rows into the JSON the dispatch map, queue, job card and
"find best slot" panel need. No writes happen here.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from ..db import jload
from ..domain.areas import area_of
from ..domain.durations import estimate_minutes
from ..domain.scoring import score_job
from ..domain.slots import Schedule, find_best_slots
from ..domain.timeutil import at_local_minutes, minutes_of_day, parse_iso, to_iso
from ..domain.travel import HaversineTravel
from .job_exceptions import exception_map, get_exception
from .settings_store import get_durations


# ------------------------------------------------------------------------- loaders

def tech_from_row(r) -> dict:
    return {
        "id": r["hcp_employee_id"], "name": r["name"], "active": bool(r["active"]),
        "trade_skills": jload(r["trade_skills"], []), "home_address": r["home_address"],
        "home_lat": r["home_lat"], "home_lng": r["home_lng"],
        "shift_start": r["shift_start"], "shift_end": r["shift_end"],
        "work_days": jload(r["work_days"], [0, 1, 2, 3, 4]), "max_jobs_per_day": r["max_jobs_per_day"],
        "color": r["color"],
    }


def load_technicians(conn) -> List[dict]:
    return [tech_from_row(r) for r in conn.execute("SELECT * FROM technicians ORDER BY name")]


def job_dict(r) -> dict:
    d = dict(r)
    d["assigned_employee_ids"] = jload(d.get("assigned_employee_ids"), [])
    d["tags"] = jload(d.get("tags"), [])
    return d


def warranty_dict(row) -> Optional[dict]:
    if not row:
        return None
    d = dict(row)
    d["data"] = jload(d.get("data"), {})
    d["parse_warnings"] = jload(d.get("parse_warnings"), [])
    return d


def _warranty_map(conn) -> Dict[str, dict]:
    return {r["hcp_job_id"]: warranty_dict(r) for r in conn.execute("SELECT * FROM warranty_details")}


def job_text(job: dict, warranty: Optional[dict]) -> str:
    items = ((warranty or {}).get("data") or {}).get("items") or []
    if items:
        return " ".join(f"{i.get('name') or ''} {i.get('problem') or ''}" for i in items)
    return (job.get("description_raw") or "")[:400]


def items_summary(job: dict, warranty: Optional[dict]) -> str:
    items = ((warranty or {}).get("data") or {}).get("items") or []
    if items:
        parts = [(i["name"] + (f" - {i['problem']}" if i.get("problem") else "")) for i in items]
        return "; ".join(parts)[:160]
    return " ".join((job.get("description_raw") or "").split())[:160]


def address_line(job: dict) -> str:
    tail = " ".join(x for x in (job.get("state"), job.get("zip")) if x)
    return ", ".join(x for x in (job.get("street"), job.get("city"), tail) if x)


def _tz(settings: dict) -> ZoneInfo:
    return ZoneInfo(settings["timezone"])


# -------------------------------------------------------------------- queue / map

def unscheduled_entry(job: dict, warranty: Optional[dict], settings: dict, now: datetime,
                      exception: Optional[dict] = None) -> dict:
    sc = score_job(job, warranty, settings, now, exception)
    area_key, area_label = area_of(job, settings["areas"]["group_by"])
    return {
        "id": job["hcp_job_id"], "customer_name": job["customer_name"], "address": address_line(job),
        "city": job["city"], "zip": job["zip"], "lat": job["lat"], "lng": job["lng"],
        "area_key": area_key, "area": area_label,
        "exception_label": exception["reason_label"] if exception else None,
        "geocode_status": job["geocode_status"], "trade_code": job["trade_code"],
        "source_category": job["source_category"], "lead_source": job["lead_source"],
        "priority_label": sc["priority_label"], "score": sc["total"], "deadline_status": sc["deadline_status"],
        "deadline_hours_left": sc["deadline_hours_left"], "age_hours": sc["age_hours"],
        "urgency_flags": sc["urgency_flags"], "summary": items_summary(job, warranty),
        "received_at": job["hcp_created_at"],
        "warranty": None if not warranty else {
            "company": warranty["warranty_company"], "dispatch_number": warranty["dispatch_number"],
            "do_not_collect_service_fee": bool(warranty["do_not_collect_service_fee"]),
            "authorization_required": warranty["authorization_required"],
            "has_warnings": bool(warranty["parse_warnings"]), "parsed_by": warranty["parsed_by"],
        },
    }


def _stop(job: dict, warranty: Optional[dict], seq: int, tz: ZoneInfo, settings: dict, durations: list) -> dict:
    start, end = parse_iso(job["scheduled_start"]), parse_iso(job["scheduled_end"])
    s_min = minutes_of_day(start, tz) if start else 0
    if end and start and end.astimezone(tz).date() == start.astimezone(tz).date():
        e_min = minutes_of_day(end, tz)
    else:
        e_min = min(24 * 60, s_min + estimate_minutes(job["trade_code"], job_text(job, warranty), durations,
                                                      settings["scheduling"]["default_duration_minutes"]))
    return {
        "id": job["hcp_job_id"], "seq": seq, "lat": job["lat"], "lng": job["lng"],
        "start_iso": job["scheduled_start"], "end_iso": job["scheduled_end"], "start_min": s_min, "end_min": e_min,
        "customer_name": job["customer_name"], "address": address_line(job), "trade_code": job["trade_code"],
        "source_category": job["source_category"], "status": job["work_status"],
        "priority_label": (warranty or {}).get("dispatch_priority") or "",
        "summary": items_summary(job, warranty),
    }


def _route_totals(home, stops: List[dict], travel: HaversineTravel) -> dict:
    pts = ([(home["lat"], home["lng"])] if home else []) + [(s["lat"], s["lng"]) for s in stops
                                                           if s["lat"] is not None and s["lng"] is not None]
    mins = sum(travel.minutes(a, b) for a, b in zip(pts, pts[1:]))
    miles = sum(travel.miles(a, b) for a, b in zip(pts, pts[1:]))
    return {"drive_min": round(mins), "miles": round(miles, 1)}


def build_dispatch(conn, d: date, settings: dict, now: datetime) -> dict:
    tz = _tz(settings)
    travel = HaversineTravel.from_settings(settings)
    durations = get_durations(conn)
    wmap = _warranty_map(conn)
    techs = load_technicians(conn)

    day_lo, day_hi = to_iso(at_local_minutes(d, 0, tz)), to_iso(at_local_minutes(d + timedelta(days=1), 0, tz))
    scheduled = [job_dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE active = 1 AND work_status IN ('scheduled','in_progress') "
        "AND scheduled_start >= ? AND scheduled_start < ? ORDER BY scheduled_start", (day_lo, day_hi))]

    by_tech: Dict[str, List[dict]] = {}
    unassigned: List[dict] = []
    for j in scheduled:
        w = wmap.get(j["hcp_job_id"])
        ids = j["assigned_employee_ids"]
        if not ids:
            unassigned.append(_stop(j, w, 0, tz, settings, durations))
            continue
        for tid in ids:
            by_tech.setdefault(tid, []).append(j)

    tech_out = []
    for t in techs:
        jobs = by_tech.get(t["id"], [])
        if not t["active"] and not jobs:
            continue                      # hide disabled techs unless they have work today
        stops = [_stop(j, wmap.get(j["hcp_job_id"]), i + 1, tz, settings, durations) for i, j in enumerate(jobs)]
        home = {"lat": t["home_lat"], "lng": t["home_lng"], "address": t["home_address"]} \
            if t["home_lat"] is not None else None
        tech_out.append({
            "id": t["id"], "name": t["name"], "color": t["color"], "active": t["active"],
            "trade_skills": t["trade_skills"], "home": home, "stops": stops, "job_count": len(stops),
            "needs_setup": t["active"] and (not t["trade_skills"] or home is None),
            **_route_totals(home, stops, travel),
        })
    known = {t["id"] for t in techs}
    for tid, jobs in by_tech.items():     # jobs assigned to someone we have no record of
        if tid not in known:
            unassigned += [_stop(j, wmap.get(j["hcp_job_id"]), 0, tz, settings, durations) for j in jobs]

    uns = []
    exceptions = exception_map(conn)
    for r in conn.execute("SELECT * FROM jobs WHERE active = 1 AND work_status = 'unscheduled'"):
        j = job_dict(r)
        uns.append(unscheduled_entry(j, wmap.get(j["hcp_job_id"]), settings, now, exceptions.get(j["hcp_job_id"])))
    uns.sort(key=lambda u: (-u["score"], u["received_at"] or ""))

    last = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "date": d.isoformat(), "today": now.astimezone(tz).date().isoformat(), "timezone": settings["timezone"],
        "now": to_iso(now), "technicians": tech_out, "unassigned_scheduled": unassigned, "unscheduled": uns,
        "stats": {
            "unscheduled": len(uns), "unmapped": sum(1 for u in uns if u["lat"] is None),
            "scheduled_today": sum(t["job_count"] for t in tech_out) + len(unassigned),
            "overdue": sum(1 for u in uns if u["deadline_status"] == "overdue"),
        },
        "last_sync": None if not last else {k: last[k] for k in ("started_at", "finished_at", "status", "mode",
                                                                  "jobs_seen", "jobs_changed", "error")},
    }


# ------------------------------------------------------------------------ detail

def _safe_http(url: Optional[str]) -> Optional[str]:
    return url if isinstance(url, str) and url.lower().startswith(("http://", "https://")) else None


def build_job_detail(conn, job_id: str, settings: dict, now: datetime) -> Optional[dict]:
    r = conn.execute("SELECT * FROM jobs WHERE hcp_job_id = ?", (job_id,)).fetchone()
    if not r:
        return None
    job = job_dict(r)
    wr = conn.execute("SELECT * FROM warranty_details WHERE hcp_job_id = ?", (job_id,)).fetchone()
    w = warranty_dict(wr)
    wd = (w or {}).get("data") or {}
    sc = score_job(job, w, settings, now, get_exception(conn, job_id))
    durations = get_durations(conn)
    names = {t["id"]: t["name"] for t in load_technicians(conn)}
    return {
        "id": job_id, "work_status": job["work_status"], "active": bool(job["active"]),
        "customer_name": job["customer_name"], "customer_phone": job["customer_phone"],
        "contact_phones": wd.get("contact_phones") or ([job["customer_phone"]] if job["customer_phone"] else []),
        "address": address_line(job), "lat": job["lat"], "lng": job["lng"], "geocode_status": job["geocode_status"],
        "trade_code": job["trade_code"], "source_category": job["source_category"], "lead_source": job["lead_source"],
        "job_type": job["job_type"], "tags": job["tags"], "received_at": job["hcp_created_at"],
        "scheduled_start": job["scheduled_start"], "scheduled_end": job["scheduled_end"],
        "assigned": [{"id": i, "name": names.get(i, i)} for i in job["assigned_employee_ids"]],
        "estimated_minutes": estimate_minutes(job["trade_code"], job_text(job, w), durations,
                                              settings["scheduling"]["default_duration_minutes"]),
        "score": sc,
        "summary": items_summary(job, w),
        "hcp_url": (settings["links"]["hcp_job_url_template"] or "").replace("{id}", job_id) or None,
        "warranty": None if not w else {
            "company": w["warranty_company"], "dispatch_number": w["dispatch_number"],
            "dispatch_priority": w["dispatch_priority"], "authorization_required": w["authorization_required"],
            "do_not_collect_service_fee": bool(w["do_not_collect_service_fee"]),
            "completion_date_required": bool(w["completion_date_required"]),
            "recall_applies": bool(wd.get("recall_applies")), "plan_name": wd.get("plan_name"),
            "payment_type": wd.get("payment_type"), "total": wd.get("total"), "paid": wd.get("paid"),
            "remaining": wd.get("remaining"), "items": wd.get("items") or [],
            "service_request_id": wd.get("service_request_id"), "contract_id": wd.get("contract_id"),
            "authorization_link": _safe_http(wd.get("authorization_link")),
            "dispatch_me_links": [u for u in (wd.get("dispatch_me_links") or []) if _safe_http(u)],
            "parsed_by": w["parsed_by"], "parse_warnings": w["parse_warnings"], "reviewed": bool(w["reviewed"]),
        },
        "description_raw": job["description_raw"],
    }


# ------------------------------------------------------------------------- slots

def build_schedule(conn, days: List[date], settings: dict, durations: list, wmap: dict) -> tuple:
    tz = _tz(settings)
    lo, hi = to_iso(at_local_minutes(days[0], 0, tz)), to_iso(at_local_minutes(days[-1] + timedelta(days=1), 0, tz))
    schedule: Schedule = {}
    unlocated = 0
    for r in conn.execute("SELECT * FROM jobs WHERE active = 1 AND work_status IN ('scheduled','in_progress') "
                          "AND scheduled_start >= ? AND scheduled_start < ?", (lo, hi)):
        j = job_dict(r)
        start = parse_iso(j["scheduled_start"])
        if not start:
            continue
        stop = _stop(j, wmap.get(j["hcp_job_id"]), 0, tz, settings, durations)
        if stop["lat"] is None:
            unlocated += 1
        d = start.astimezone(tz).date()
        for tid in j["assigned_employee_ids"]:
            schedule.setdefault((tid, d), []).append(
                {"id": stop["id"], "lat": stop["lat"], "lng": stop["lng"], "start_min": stop["start_min"],
                 "end_min": stop["end_min"], "label": stop["customer_name"]})
    return schedule, unlocated


def compute_slots(conn, job_id: str, settings: dict, now: datetime, search_days: Optional[int] = None) -> Optional[dict]:
    r = conn.execute("SELECT * FROM jobs WHERE hcp_job_id = ?", (job_id,)).fetchone()
    if not r:
        return None
    job = job_dict(r)
    if job["work_status"] != "unscheduled":
        raise ValueError("Slots can only be suggested for unscheduled jobs")
    tz = _tz(settings)
    n_days = max(1, min(14, int(search_days or settings["scheduling"]["search_days"])))
    today = now.astimezone(tz).date()
    days = [today + timedelta(days=i) for i in range(n_days)]
    durations = get_durations(conn)
    wmap = _warranty_map(conn)
    w = wmap.get(job_id)
    duration = estimate_minutes(job["trade_code"], job_text(job, w), durations,
                                settings["scheduling"]["default_duration_minutes"])
    sc = score_job(job, w, settings, now, get_exception(conn, job_id))
    excused = sc["deadline_status"] == "excused"
    schedule, unlocated = build_schedule(conn, days, settings, durations, wmap)
    result = find_best_slots(
        {"id": job_id, "lat": job["lat"], "lng": job["lng"], "trade_code": job["trade_code"]},
        load_technicians(conn), schedule, HaversineTravel.from_settings(settings), settings, now, days, duration, tz,
        priority_label=sc["priority_label"], deadline_at=None if excused else parse_iso(sc["deadline_at"]))
    if excused:
        result["notes"].append(f"Marked as scheduled outside the window ({sc['exception']['reason_label']}), "
                               "so the deadline is not used to rank these options.")
    if unlocated:
        result["notes"].append(f"{unlocated} scheduled stop(s) have no map location, so drive time to/from them is "
                               "assumed to be zero.")
    result.update({"job_id": job_id, "duration_min": duration, "search_days": n_days,
                   "priority_label": sc["priority_label"], "deadline_at": sc["deadline_at"],
                   "travel_model": "straight-line distance estimate (swap in Google/Mapbox for road times)"})
    return result


# ------------------------------------------------------------------------- areas

def build_areas(conn, settings: dict, now: datetime, search_days: Optional[int] = None) -> dict:
    """Running total of unscheduled calls per area, plus who has the soonest opening in each.

    Availability is the cheapest-insertion engine in "soonest" mode, run for every mapped call on its own
    (so a call is only counted for a technician with its trade skill and a gap long enough for it). The calls
    share the same technicians, so openings are a guide to where to send someone, not a booking plan.
    """
    tz = _tz(settings)
    n_days = max(1, min(14, int(search_days or settings["scheduling"]["search_days"])))
    today = now.astimezone(tz).date()
    days = [today + timedelta(days=i) for i in range(n_days)]
    group_by = settings["areas"]["group_by"]
    durations = get_durations(conn)
    wmap = _warranty_map(conn)
    techs = load_technicians(conn)
    exceptions = exception_map(conn)
    schedule, _ = build_schedule(conn, days, settings, durations, wmap)
    travel = HaversineTravel.from_settings(settings)
    max_opts = max(1, len(techs) * n_days)
    routable = [t for t in techs if t["active"] and t["trade_skills"] and t["home_lat"] is not None]

    areas: Dict[str, dict] = {}
    total = 0
    for r in conn.execute("SELECT * FROM jobs WHERE active = 1 AND work_status = 'unscheduled'"):
        job = job_dict(r)
        jid = job["hcp_job_id"]
        w = wmap.get(jid)
        sc = score_job(job, w, settings, now, exceptions.get(jid))
        key, label = area_of(job, group_by)
        a = areas.setdefault(key, {"key": key, "label": label, "count": 0, "by_trade": {}, "overdue": 0, "due_soon": 0,
                                   "excused": 0, "unlocated": 0, "oldest_received_at": None, "_techs": {}})
        total += 1
        a["count"] += 1
        trade = job["trade_code"] or "other"
        a["by_trade"][trade] = a["by_trade"].get(trade, 0) + 1
        a["overdue"] += sc["deadline_status"] == "overdue"
        a["due_soon"] += sc["deadline_status"] in ("critical", "warning")
        a["excused"] += sc["deadline_status"] == "excused"
        received = job["hcp_created_at"]
        if received and (a["oldest_received_at"] is None or received < a["oldest_received_at"]):
            a["oldest_received_at"] = received
        if job["lat"] is None or job["lng"] is None or not routable:
            a["unlocated"] += job["lat"] is None or job["lng"] is None
            continue
        duration = estimate_minutes(job["trade_code"], job_text(job, w), durations,
                                    settings["scheduling"]["default_duration_minutes"])
        res = find_best_slots({"id": jid, "lat": job["lat"], "lng": job["lng"], "trade_code": job["trade_code"]},
                              techs, schedule, travel, settings, now, days, duration, tz,
                              soonest=True, limit=max_opts)
        seen = set()
        for o in res["options"]:                       # sorted soonest first: the first hit per tech is its earliest
            if o["tech_id"] in seen:
                continue
            seen.add(o["tech_id"])
            t = a["_techs"].setdefault(o["tech_id"], {
                "tech_id": o["tech_id"], "name": o["tech_name"], "color": o["tech_color"], "date": o["date"],
                "start_min": o["start_min"], "start_iso": o["start_iso"], "added_drive_min": o["added_drive_min"],
                "job_id": jid, "eligible_jobs": 0})
            t["eligible_jobs"] += 1
            if (o["date"], o["start_min"]) < (t["date"], t["start_min"]):
                t.update(date=o["date"], start_min=o["start_min"], start_iso=o["start_iso"],
                         added_drive_min=o["added_drive_min"], job_id=jid)

    out = []
    for a in areas.values():
        techs_out = sorted(a.pop("_techs").values(), key=lambda t: (t["date"], t["start_min"], t["name"]))
        a["techs"] = techs_out[:5]
        a["earliest"] = techs_out[0] if techs_out else None
        out.append(a)
    out.sort(key=lambda a: (-a["count"], a["label"].casefold()))
    notes = []
    if not routable:
        notes.append("No technician is set up for routing yet, so openings cannot be shown (Admin > Technicians).")
    return {"group_by": group_by, "days": n_days, "today": today.isoformat(), "areas": out,
            "totals": {"unscheduled": total, "areas": len(out), "with_opening": sum(1 for a in out if a["earliest"])},
            "notes": notes}
