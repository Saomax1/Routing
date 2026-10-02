"""
Priority scoring for unscheduled jobs (spec 7.4).

    score = base (by priority / source)
          + deadline points (as the warranty contact/schedule deadline approaches)
          + urgency points (per keyword, capped)
          + age points (per day unscheduled, capped)

Every component is returned in ``breakdown`` so dispatchers can see *why* a job ranks where it does.
All weights and deadline rules come from settings (Admin > Settings); nothing is hard-coded here.

Deadlines are targets, not hard limits. A job whose deadline a dispatcher has waived
(``exception``) keeps its deadline time for reference but is reported as ``excused``: no deadline points and
it never counts as overdue.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from .timeutil import parse_iso, to_iso
from .warranty_parser import find_urgency_flags

CATEGORY_RULE_KEY = {"ahs": "AHS", "other_warranty": "OTHER_WARRANTY", "direct": "DIRECT"}


def priority_label(job: dict, warranty: Optional[dict]) -> str:
    """Emergency / Expedited / Normal for warranty jobs with a parsed priority, else 'Direct' or 'Normal'."""
    if warranty and warranty.get("dispatch_priority") in ("Emergency", "Expedited", "Normal"):
        return warranty["dispatch_priority"]
    return "Direct" if job.get("source_category", "direct") == "direct" else "Normal"


def urgency_text(job: dict, warranty: Optional[dict]) -> str:
    data = (warranty or {}).get("data") or {}
    items = data.get("items") or []
    if items:
        return " ".join(f"{i.get('name') or ''} {i.get('problem') or ''}" for i in items)
    return (job.get("description_raw") or "")[:600]


def deadline_hours(settings: dict, category: str, label: str) -> Optional[float]:
    rules = settings.get("deadline_rules", {}).get(CATEGORY_RULE_KEY.get(category, "DIRECT"), {})
    key = "Normal" if label == "Direct" else label
    hours = rules.get(key)
    if hours is None and label == "Direct":
        hours = rules.get("Normal")
    return float(hours) if hours else None


def deadline_status(fraction_left: Optional[float], remaining_h: Optional[float]) -> str:
    if fraction_left is None or remaining_h is None:
        return "none"
    if remaining_h <= 0:
        return "overdue"
    if fraction_left <= 0.25:
        return "critical"
    if fraction_left <= 0.5:
        return "warning"
    return "ok"


def score_job(job: dict, warranty: Optional[dict], settings: dict, now: datetime,
              exception: Optional[dict] = None) -> dict:
    sc = settings["scoring"]
    category = job.get("source_category", "direct")
    label = priority_label(job, warranty)
    breakdown = []

    # --- base
    if category == "direct":
        base, base_label = sc["base_direct_lead"], "Direct lead"
    elif warranty and warranty.get("dispatch_priority") in sc["base_by_priority"]:
        base, base_label = sc["base_by_priority"][warranty["dispatch_priority"]], f"Priority: {warranty['dispatch_priority']}"
    elif category == "other_warranty":
        base, base_label = sc["base_other_warranty"], "Other warranty job"
    else:
        base, base_label = sc["base_by_priority"]["Normal"], "Warranty (priority unknown, treated as Normal)"
    breakdown.append({"label": base_label, "points": float(base)})

    # --- deadline
    received = parse_iso(job.get("hcp_created_at"))
    window_h = deadline_hours(settings, category, label)
    deadline_at = remaining_h = fraction_left = None
    if received and window_h:
        deadline_at = received + timedelta(hours=window_h)
        remaining_h = (deadline_at - now).total_seconds() / 3600.0
        fraction_left = remaining_h / window_h
    status = deadline_status(fraction_left, remaining_h)
    if exception and status != "none":
        status = "excused"
        breakdown.append({"label": f"Deadline waived: {exception.get('reason_label') or 'excused'} (no deadline points)",
                          "points": 0.0})
    dp = sc["deadline_points"].get(status, 0)
    if dp:
        breakdown.append({"label": f"Deadline {status}", "points": float(dp)})

    # --- urgency keywords
    flags = find_urgency_flags(urgency_text(job, warranty), settings.get("urgency_keywords"))
    if flags:
        pts = min(len(flags) * sc["urgency_points_each"], sc["urgency_points_cap"])
        breakdown.append({"label": "Urgency: " + ", ".join(flags), "points": float(pts)})

    # --- age
    age_h = None
    if received:
        age_h = max(0.0, (now - received).total_seconds() / 3600.0)
        age_pts = round(min(age_h / 24.0 * sc["age_points_per_day"], sc["age_points_cap"]), 1)
        if age_pts > 0:
            breakdown.append({"label": f"Waiting {age_h / 24.0:.1f} days" if age_h >= 24 else f"Waiting {age_h:.0f} h",
                              "points": age_pts})

    return {
        "total": round(sum(b["points"] for b in breakdown), 1),
        "breakdown": breakdown,
        "priority_label": label,
        "deadline_at": to_iso(deadline_at),
        "deadline_hours_left": None if remaining_h is None else round(remaining_h, 1),
        "deadline_status": status,
        "age_hours": None if age_h is None else round(age_h, 1),
        "urgency_flags": flags,
        "exception": exception,
    }
