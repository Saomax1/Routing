"""
"Find best slot" - cheapest insertion (spec 7.3).

For one unscheduled job, look at every eligible technician on every day in the search window and
try the job in every gap of that tech's route:

    prev stop (or home base) -> NEW JOB -> next stop

Existing jobs keep their scheduled start times (they are customer promises already in HCP), so an
insertion is feasible only if:
  * the tech has the trade skill, works that weekday, is active, and is under max jobs/day
  * the new job starts after the previous stop ends + drive time (and not before shift start / now)
  * the new job ends inside the shift
  * the tech can still reach the next stop by its scheduled start

Cost = added drive minutes + a per-day delay penalty that depends on priority (so an Emergency job
prefers today even if it adds more driving) + a penalty if the slot misses the job's deadline.
Only the best option per (tech, day) is kept, then the top N overall are returned.

``soonest=True`` ranks by start time instead (earlier day first, then earlier start, drive time only breaks
ties). The Areas tab uses it to answer "who has an opening first?".

Pure functions only - no database, no network. Travel time comes from a ``TravelTimeProvider``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .timeutil import at_local_minutes, ceil_to, hhmm_to_minutes, minutes_of_day, to_iso
from .travel import TravelTimeProvider

Stop = dict  # {"id","lat","lng","start_min","end_min","label"}
Schedule = Dict[Tuple[str, date], List[Stop]]


def _loc(x: dict) -> Optional[Tuple[float, float]]:
    if x.get("lat") is None or x.get("lng") is None:
        return None
    return float(x["lat"]), float(x["lng"])


def find_best_slots(
    job: dict,
    technicians: List[dict],
    schedule: Schedule,
    travel: TravelTimeProvider,
    settings: dict,
    now: datetime,
    days: List[date],
    duration_min: int,
    tz: ZoneInfo,
    priority_label: str = "Normal",
    deadline_at: Optional[datetime] = None,
    soonest: bool = False,
    limit: Optional[int] = None,
) -> dict:
    cfg = settings["scheduling"]
    step = int(cfg.get("round_to_minutes", 5))
    top_n = int(limit if limit is not None else cfg.get("top_n", 5))
    penalty_per_day = float(cfg["day_penalty_minutes"].get(priority_label, 15))
    miss_penalty = float(cfg.get("deadline_miss_penalty", 300))
    lead = int(cfg.get("same_day_lead_minutes", 30))

    # A deadline that has already passed makes every option "late" equally, so flagging/penalising it would only
    # add noise. Rank by soonest + cheapest instead (the per-day delay penalty still pushes urgent jobs earlier).
    deadline_passed = bool(deadline_at and deadline_at <= now)
    if deadline_passed:
        deadline_at = None

    job_loc = _loc(job)
    if job_loc is None:
        return {"options": [], "ineligible": [], "notes": ["This job has no map location yet, so slots cannot be computed."]}

    trade = (job.get("trade_code") or "").upper()
    now_local = now.astimezone(tz)
    today = now_local.date()
    now_min = now_local.hour * 60 + now_local.minute

    best_per_slot: List[dict] = []
    ineligible: List[dict] = []

    for tech in technicians:
        tid = tech["id"]
        reasons: List[str] = []
        if not tech.get("active", True):
            ineligible.append({"tech_id": tid, "name": tech["name"], "reason": "Inactive"})
            continue
        skills = [s.upper() for s in tech.get("trade_skills", [])]
        if not skills:
            ineligible.append({"tech_id": tid, "name": tech["name"], "reason": "No trade skills set (Admin > Technicians)"})
            continue
        if trade and trade not in skills:
            ineligible.append({"tech_id": tid, "name": tech["name"], "reason": f"No {trade} skill"})
            continue

        shift_start = hhmm_to_minutes(tech.get("shift_start", "08:00"), 480)
        shift_end = hhmm_to_minutes(tech.get("shift_end", "17:00"), 1020)
        work_days = set(tech.get("work_days", [0, 1, 2, 3, 4]))
        max_jobs = int(tech.get("max_jobs_per_day", 6))
        home = _loc({"lat": tech.get("home_lat"), "lng": tech.get("home_lng")})
        tech_had_option = False

        for day_idx, d in enumerate(days):
            if d.weekday() not in work_days:
                reasons.append("not working")
                continue
            stops = sorted(schedule.get((tid, d), []), key=lambda s: s["start_min"])
            if len(stops) >= max_jobs:
                reasons.append("fully booked")
                continue
            earliest = shift_start
            if d == today:
                earliest = max(shift_start, ceil_to(now_min + lead, step))
            if earliest + duration_min > shift_end:
                reasons.append("no time left")
                continue

            best: Optional[dict] = None
            for pos in range(len(stops) + 1):
                prev = stops[pos - 1] if pos > 0 else None
                nxt = stops[pos] if pos < len(stops) else None
                prev_loc = _loc(prev) if prev else home
                nxt_loc = _loc(nxt) if nxt else None

                t_in = travel.minutes(prev_loc, job_loc) if prev_loc else 0.0
                depart = prev["end_min"] if prev else earliest
                arrive = ceil_to(max(depart + t_in, earliest), step)
                end = arrive + duration_min
                if end > shift_end:
                    continue
                t_out = travel.minutes(job_loc, nxt_loc) if nxt_loc else 0.0
                if nxt and end + t_out > nxt["start_min"]:
                    continue

                if prev_loc and nxt_loc:
                    added = t_in + t_out - travel.minutes(prev_loc, nxt_loc)
                else:
                    added = t_in + t_out
                added = max(0.0, added)

                end_dt = at_local_minutes(d, end, tz)
                misses = bool(deadline_at and end_dt > deadline_at)
                if soonest:
                    cost = day_idx * 24 * 60 + arrive + added * 0.01
                else:
                    cost = added + day_idx * penalty_per_day + (miss_penalty if misses else 0.0) + arrive * 0.001

                if best is None or cost < best["cost"]:
                    new_stop = {"id": job["id"], "lat": job_loc[0], "lng": job_loc[1],
                                "start_min": arrive, "end_min": end, "label": "NEW", "is_new": True}
                    preview = stops[:pos] + [new_stop] + stops[pos:]
                    best = {
                        "tech_id": tid, "tech_name": tech["name"], "tech_color": tech.get("color"),
                        "date": d.isoformat(), "position": pos + 1, "stops_in_day": len(stops),
                        "after_stop_id": prev["id"] if prev else None,
                        "before_stop_id": nxt["id"] if nxt else None,
                        "start_min": arrive, "end_min": end,
                        "start_iso": to_iso(at_local_minutes(d, arrive, tz)),
                        "end_iso": to_iso(end_dt),
                        "drive_in_min": round(t_in, 1), "drive_out_min": round(t_out, 1),
                        "added_drive_min": round(added, 1),
                        "misses_deadline": misses, "cost": round(cost, 2),
                        "home": {"lat": home[0], "lng": home[1]} if home else None,
                        "route_preview": [{"id": s["id"], "lat": s.get("lat"), "lng": s.get("lng"),
                                           "start_min": s["start_min"], "end_min": s["end_min"],
                                           "is_new": bool(s.get("is_new"))} for s in preview],
                    }
            if best:
                best_per_slot.append(best)
                tech_had_option = True
            else:
                reasons.append("no gap")

        if not tech_had_option:
            if reasons and all(r == "not working" for r in reasons):
                why = "Not working in the search window"
            elif "no gap" in reasons:
                why = "No gap long enough (including drive time) in the search window"
            elif reasons:
                why = "Fully booked / no time left in the search window"
            else:
                why = "No options"
            ineligible.append({"tech_id": tid, "name": tech["name"], "reason": why})

    best_per_slot.sort(key=lambda o: o["cost"])
    notes = []
    if deadline_passed:
        notes.append("This job is already past its scheduling window, so options are ranked by how soon and how cheaply it can be done.")
    if not best_per_slot:
        notes.append("No feasible slot found in the search window. Try a longer window or adjust technician hours/skills.")
    return {"options": best_per_slot[:top_n], "ineligible": ineligible, "notes": notes}
