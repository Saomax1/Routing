"""
"Find best slot" - cheapest insertion into arrival windows (spec 7.3).

A customer is not given a time but a WINDOW ("we will arrive between 8 AM and 12 PM", 4 hours by default). Windows
are allowed to overlap: two jobs can share 8-12, or job 1 can be 8-12 and job 2 10-2. That is what makes dispatching
easier: the technician only has to arrive inside each window, and jobs that run long are absorbed by the slack.

For one unscheduled job, look at every eligible technician on every day in the search window and try the job at
every position of that tech's route:

    origin -> ... -> prev stop -> NEW JOB -> next stop -> ...

where ``origin`` is the tech's home base, or - for today - the last job they already completed.

A position is feasible when, driving and working straight through from the start of the day (or from now), every open
stop is still reached inside its window and the day ends inside the shift. A stop that the plan cannot reach in time
even WITHOUT the new job (our travel times are estimates) is tolerated at the arrival it already has, so inserting a
job may never make it later, but we do not refuse the whole day over a schedule that already exists in HCP.
Completed and in-progress jobs are not re-planned; they only count toward the daily maximum, and an in-progress job
keeps the technician busy until it ends.

The window offered for the new job is chosen from its planned arrival time (``choose_window``). If a nearby job (within
``stack_within_minutes`` of driving) already has a window that contains the arrival time, the new job is offered the
SAME window start, and the option says so (``stacked_with``).

Cost = added drive minutes + a per-day delay penalty that depends on priority (so an Emergency job prefers today even
if it adds more driving) + a penalty if the job would finish after its deadline. Only the best option per (tech, day)
is kept, then the top N overall are returned. ``soonest=True`` ranks by planned arrival instead (earlier day first,
then earlier arrival, drive time only breaks ties); the Areas tab uses it to answer "who has an opening first?".

A stop without window fields has a zero-width window (a fixed time), so older callers behave as before.

Pure functions only - no database, no network. Travel time comes from a ``TravelTimeProvider``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .timeutil import at_local_minutes, ceil_to, hhmm_to_minutes, to_iso
from .travel import TravelTimeProvider

Stop = dict  # {"id","lat","lng","start_min","end_min","label", optional "win_start_min","win_end_min","state"}
Schedule = Dict[Tuple[str, date], List[Stop]]

COMMITTED = ("in_progress", "complete")      # already under way or done: never re-planned
INF = float("inf")


def _loc(x: dict) -> Optional[Tuple[float, float]]:
    if x.get("lat") is None or x.get("lng") is None:
        return None
    return float(x["lat"]), float(x["lng"])


def choose_window(eta: float, shift_start: int, shift_end: int, window: int, grid: int,
                  min_start: Optional[int] = None) -> Tuple[int, int]:
    """The arrival window (start, end) in minutes of the day to offer for a planned arrival at ``eta``.

    It starts on the latest grid point at or before the arrival (grid 60: arrive 8:20 -> 8-12, arrive 10:20 -> 10-2),
    is pulled earlier so the whole window still fits inside the shift (arrive 3:10 PM -> 1-5 PM), and never starts
    before the technician does or before ``min_start`` (for today: not in the past).
    """
    grid = max(1, int(grid))
    ws = int(eta // grid) * grid
    ws = min(ws, shift_end - window)
    ws = max(ws, shift_start, min_start if min_start is not None else shift_start)
    ws = min(ws, int(eta))
    if ws + window < eta:                        # a window shorter than the grid step: start at the arrival itself
        ws = int(eta)
    return ws, min(ws + window, shift_end)       # the arrival is always inside its own window


def _window(s: Stop) -> Tuple[int, int]:
    ws = s.get("win_start_min", s["start_min"])
    return ws, s.get("win_end_min", ws)


def _overlaps(ws: int, we: int, other: Stop) -> bool:
    """Do the windows share any time? Windows that only touch (one ends 9:00, the other starts 9:00) do not; a stop
    with no window (a fixed time) overlaps if that moment falls inside."""
    a, b = _window(other)
    return ws <= a < we if a == b else ws < b and a < we


def _order_key(s: Stop):
    ws, _ = _window(s)
    return ws, s["start_min"], str(s.get("id"))


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
    window_min: Optional[int] = None,
) -> dict:
    cfg = settings["scheduling"]
    step = int(cfg.get("round_to_minutes", 5))
    top_n = int(limit if limit is not None else cfg.get("top_n", 5))
    penalty_per_day = float(cfg["day_penalty_minutes"].get(priority_label, 15))
    miss_penalty = float(cfg.get("deadline_miss_penalty", 300))
    lead = int(cfg.get("same_day_lead_minutes", 30))
    window = int(window_min or cfg.get("window_minutes", 240))
    grid = int(cfg.get("window_step_minutes", 60))
    stack_within = float(cfg.get("stack_within_minutes", 20))

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

    def tt(a, b) -> float:
        return travel.minutes(a, b) if a and b else 0.0

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
            all_stops = schedule.get((tid, d), [])
            if len(all_stops) >= max_jobs:                       # completed jobs count: they were done today
                reasons.append("fully booked")
                continue

            avail = shift_start
            if d == today:
                avail = max(shift_start, ceil_to(now_min + lead, step))
            origin, origin_kind = home, "home"
            committed = [s for s in all_stops if s.get("state") in COMMITTED]
            if committed:
                last = max(committed, key=lambda s: s["end_min"])
                if _loc(last):
                    origin, origin_kind = _loc(last), last["state"]
                if last["state"] == "in_progress":
                    avail = max(avail, last["end_min"])          # busy until the job under way is finished
            if avail + duration_min > shift_end:
                reasons.append("no time left")
                continue

            seq = sorted((s for s in all_stops if s.get("state") not in COMMITTED), key=_order_key)

            # The plan without the new job: how late each open stop would be reached. A stop that is already late
            # (or whose window closed before we are free) is tolerated at that arrival, never made later.
            t, here, limits = avail, origin, []
            for s in seq:
                ws, we = _window(s)
                t = max(t + tt(here, _loc(s)), ws)
                limits.append(INF if we < avail else max(we, t))
                t += s["end_min"] - s["start_min"]
                here = _loc(s)

            min_start = (now_min // grid) * grid if d == today else shift_start
            best: Optional[dict] = None
            for pos in range(len(seq) + 1):
                prev_loc = (_loc(seq[pos - 1]) if pos > 0 else origin)
                nxt = seq[pos] if pos < len(seq) else None
                nxt_loc = _loc(nxt) if nxt else None

                # forward plan with the new job at `pos`
                t, here, plan, eta, feasible = avail, origin, [], None, True
                order = [(s, i) for i, s in enumerate(seq[:pos])] + [(None, -1)] + [(s, i + pos) for i, s in enumerate(seq[pos:])]
                for s, i in order:
                    if s is None:
                        arr = ceil_to(t + tt(here, job_loc), step)
                        eta, dur, here = arr, duration_min, job_loc
                    else:
                        arr = max(t + tt(here, _loc(s)), _window(s)[0])
                        if arr > limits[i]:
                            feasible = False
                            break
                        dur, here = s["end_min"] - s["start_min"], _loc(s)
                    plan.append((s, arr, arr + dur))
                    t = arr + dur
                if not feasible or t > shift_end:
                    continue

                t_in = tt(prev_loc, job_loc)
                t_out = tt(job_loc, nxt_loc)
                added = max(0.0, t_in + t_out - tt(prev_loc, nxt_loc)) if prev_loc and nxt_loc else t_in + t_out

                end = eta + duration_min
                end_dt = at_local_minutes(d, end, tz)
                misses = bool(deadline_at and end_dt > deadline_at)
                if soonest:
                    cost = day_idx * 24 * 60 + eta + added * 0.01
                else:
                    cost = added + day_idx * penalty_per_day + (miss_penalty if misses else 0.0) + eta * 0.001

                if best is not None and cost >= best["cost"]:
                    continue

                # the window to offer: on the grid, or - for a nearby job whose window already holds the arrival -
                # the same window start as that job
                ws, we = choose_window(eta, shift_start, shift_end, window, grid, min_start)
                near = []
                for s in seq:
                    dm = tt(job_loc, _loc(s)) if _loc(s) else None
                    if dm is not None and dm <= stack_within:
                        near.append((dm, s))
                snap = [(dm, s) for dm, s in near
                        if _window(s)[0] <= eta <= max(_window(s)[1], _window(s)[0])
                        and min_start <= _window(s)[0] <= eta <= _window(s)[0] + window]
                if snap:
                    dm, s = min(snap, key=lambda x: (x[0], _window(x[1])[0]))
                    ws = _window(s)[0]
                    we = min(ws + window, shift_end)
                stacked = [{"id": s["id"], "label": s.get("label"), "drive_min": round(dm, 1),
                            "same_window": _window(s)[0] == ws,
                            "window_start_min": _window(s)[0], "window_end_min": _window(s)[1]}
                           for dm, s in near if _overlaps(ws, we, s)]

                new_stop = {"id": job["id"], "lat": job_loc[0], "lng": job_loc[1], "start_min": eta, "end_min": end,
                            "label": "NEW", "is_new": True}
                preview = [{"id": (s or new_stop)["id"], "lat": (s or new_stop).get("lat"), "lng": (s or new_stop).get("lng"),
                            "start_min": a, "end_min": e, "is_new": s is None} for s, a, e in plan]
                best = {
                    "tech_id": tid, "tech_name": tech["name"], "tech_color": tech.get("color"),
                    "date": d.isoformat(), "position": len(committed) + pos + 1,   # in the whole day, finished jobs first
                    "stops_in_day": len(all_stops),
                    "after_stop_id": seq[pos - 1]["id"] if pos > 0 else None,
                    "before_stop_id": nxt["id"] if nxt else None,
                    "start_min": eta, "end_min": end,                              # planned arrival and finish
                    "start_iso": to_iso(at_local_minutes(d, eta, tz)), "end_iso": to_iso(end_dt),
                    "window_start_min": ws, "window_end_min": we, "window_minutes": we - ws,
                    "window_start_iso": to_iso(at_local_minutes(d, ws, tz)),
                    "window_end_iso": to_iso(at_local_minutes(d, we, tz)),
                    "stacked_with": stacked,
                    "drive_in_min": round(t_in, 1), "drive_out_min": round(t_out, 1),
                    "added_drive_min": round(added, 1),
                    "misses_deadline": misses, "cost": round(cost, 2),
                    "home": {"lat": home[0], "lng": home[1]} if home else None,
                    "origin": {"lat": origin[0], "lng": origin[1], "kind": origin_kind} if origin else None,
                    "route_preview": preview,
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
        notes.append("This job is already past its deadline, so options are ranked by how soon and how cheaply it can be done.")
    if not best_per_slot:
        notes.append("No feasible slot found in the search window. Try a longer window or adjust technician hours/skills.")
    return {"options": best_per_slot[:top_n], "ineligible": ineligible, "notes": notes, "window_minutes": window}
