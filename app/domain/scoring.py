"""
Priority scoring for unscheduled jobs (spec 7.4).

    score = base (by type: Expedited / Recall / Normal warranty work, or Retail)
          + deadline points (as the contact/schedule deadline approaches)
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

from .jobkind import WARRANTY, classify_job
from .timeutil import parse_iso, to_iso
from .warranty_parser import find_urgency_flags

RULE_KEY = {"warranty": "WARRANTY", "retail": "RETAIL"}


def urgency_text(job: dict, warranty: Optional[dict]) -> str:
    data = (warranty or {}).get("data") or {}
    items = data.get("items") or []
    if items:
        return " ".join(f"{i.get('name') or ''} {i.get('problem') or ''}" for i in items)
    return (job.get("description_raw") or "")[:600]


def deadline_hours(settings: dict, kind: str, label: str) -> Optional[float]:
    """Hours allowed for this type (kind = "warranty" | "retail", label = Expedited / Normal / Recall / Retail)."""
    hours = settings.get("deadline_rules", {}).get(RULE_KEY.get(kind, "RETAIL"), {}).get(label)
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
    kind = classify_job(job, settings, warranty)
    label = kind["label"]
    breakdown = []

    # --- base
    if kind["kind"] == WARRANTY:
        base, base_label = sc["base_by_priority"].get(label, sc["base_by_priority"]["Normal"]), f"Warranty: {label}"
    else:
        base, base_label = sc["base_retail"], "Retail (ad lead)" if kind["ad_lead"] else "Retail"
    breakdown.append({"label": base_label, "points": float(base)})

    # --- deadline
    received = parse_iso(job.get("hcp_created_at"))
    window_h = deadline_hours(settings, kind["kind"], label)
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
        "kind": kind["kind"],
        "ad_lead": kind["ad_lead"],
        "warranty_text_without_tag": kind["warranty_text_without_tag"],
        "deadline_at": to_iso(deadline_at),
        "deadline_hours_left": None if remaining_h is None else round(remaining_h, 1),
        "deadline_status": status,
        "age_hours": None if age_h is None else round(age_h, 1),
        "urgency_flags": flags,
        "exception": exception,
    }
