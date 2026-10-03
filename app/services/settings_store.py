"""
Admin-editable settings (stored as one JSON document in the ``settings`` table), with defaults.

``deadline_rules`` control the deadline warnings and the urgency points in the priority score. They are
TARGETS, not hard limits: a dispatcher can waive a job's deadline and record why (see
``services/job_exceptions.py``). A type with no rule simply has no deadline clock. Normal warranty calls are
48 h; the Expedited, Recall and Retail values are still placeholders - confirm them under Admin > Settings.

Job types (``job_types``): warranty work is Expedited, Normal or Recall, told apart by three Housecall Pro tags;
everything else is Retail (``domain/jobkind.py``). The tag texts are editable here.
"""

from __future__ import annotations

import copy
import math
from typing import Any

from ..db import jdump, jload
from ..domain.areas import GROUP_BY
from ..domain.durations import DEFAULT_DURATIONS
from ..domain.jobkind import DEFAULT_JOB_TYPES, validate_job_types

SETTINGS_KEY = "app"

DEFAULT_SETTINGS: dict = {
    "timezone": "America/Phoenix",
    "scoring": {
        # Warranty work by type. Recall (a return visit to something already repaired) is a placeholder: confirm it.
        "base_by_priority": {"Expedited": 60, "Recall": 40, "Normal": 20},
        "base_retail": 30,
        "urgency_points_each": 10,
        "urgency_points_cap": 30,
        "age_points_per_day": 3,
        "age_points_cap": 30,
        "deadline_points": {"overdue": 40, "critical": 30, "warning": 15},
    },
    # Hours from "received" until the job should be scheduled/contacted. A target, not a hard limit (jobs can be
    # given a "deadline waived" note). Normal warranty = 48 h; Expedited, Recall and Retail are placeholders: confirm!
    "deadline_rules": {
        "WARRANTY": {"Expedited": 24, "Normal": 48, "Recall": 24},
        "RETAIL": {"Retail": 24},
    },
    # Which Housecall Pro tags make a job Expedited / Normal / Recall warranty work (anything else is Retail), and
    # which tag marks a retail lead from an ad. Compared ignoring case and spacing.
    "job_types": DEFAULT_JOB_TYPES,
    # How the running totals on the Areas tab group unscheduled calls: by "city" or by "zip" code.
    "areas": {"group_by": "city"},
    "urgency_keywords": ["secondary damage", "leak", "flood", "no water", "no hot water",
                         "no heat", "no cooling", "no ac", "gas", "sewage", "backup", "burst"],
    "trade_aliases": {
        "PLB": "PLB", "PLUMBING": "PLB", "PLUMBER": "PLB",
        "HVAC": "HVAC", "HVC": "HVAC", "HVA": "HVAC", "HEATING": "HVAC", "COOLING": "HVAC", "AC": "HVAC",
        "ELE": "ELEC", "ELEC": "ELEC", "ELECTRICAL": "ELEC",
        "APP": "APPL", "APPL": "APPL", "APPLIANCE": "APPL",
    },
    "scheduling": {
        "search_days": 3,
        "top_n": 5,
        "default_duration_minutes": 60,
        "round_to_minutes": 5,
        # Extra "cost" (in minutes of driving) per day of delay, by type: Expedited jobs prefer today
        "day_penalty_minutes": {"Expedited": 90, "Recall": 60, "Normal": 15, "Retail": 10},
        "deadline_miss_penalty": 300,
        "travel_speed_mph": 28,
        "travel_circuity": 1.3,     # straight-line distance x this ~= road distance
        "min_travel_minutes": 3,
        "same_day_lead_minutes": 30,  # don't suggest a start sooner than this from now
        # Arrival windows: the customer is told "we'll arrive between X and X + window". Windows may overlap.
        "window_minutes": 240,        # standard window (4 h); can also be changed per search on the job card
        "window_step_minutes": 60,    # windows start on this grid (60 = on the hour: 8-12, 9-1, 10-2 ...)
        "stack_within_minutes": 20,   # a nearby job (<= this much driving) is suggested into the same window
    },
    "map": {
        "tile_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap contributors",
        "center": [33.30, -111.80],
        "zoom": 10,
    },
    # UNVERIFIED URL pattern - check a real HCP job link and adjust ({id} is replaced).
    "links": {"hcp_job_url_template": "https://pro.housecallpro.com/app/jobs/{id}"},
}


# Allowed range of each slot-finder number: outside it the finder returns nothing, or nonsense.
SCHEDULING_LIMITS = {
    "search_days": (1, 14), "top_n": (1, 50), "default_duration_minutes": (5, 720), "round_to_minutes": (1, 60),
    "travel_speed_mph": (5, 100), "travel_circuity": (1, 3), "min_travel_minutes": (0, 60),
    "same_day_lead_minutes": (0, 720), "deadline_miss_penalty": (0, 10000),
    "window_minutes": (15, 720), "window_step_minutes": (5, 240), "stack_within_minutes": (0, 120),
}

# Free-form maps that users add to and remove from. A saved copy replaces the default instead of merging into it,
# otherwise a rule removed in Admin > Settings would come back from the defaults on the next read.
FREE_FORM_MAPS = ("deadline_rules", "trade_aliases")


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _check(default: Any, value: Any, path: str) -> None:
    """Light type validation against the defaults; raises ValueError with a readable path."""
    if isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, (int, float)):
        # NaN / Infinity are accepted by the JSON parser, but a stored one cannot be sent back to the browser: the
        # Settings page would no longer load
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    elif isinstance(default, str):
        ok = isinstance(value, str)
    elif isinstance(default, list):
        ok = isinstance(value, list)
    elif isinstance(default, dict):
        ok = isinstance(value, dict)
        if ok:
            # dict-of-dicts that users extend freely (company -> priority -> hours, alias map)
            free = path in ("deadline_rules", "trade_aliases") or path.startswith("deadline_rules.")
            for k, v in value.items():
                if k in default:
                    _check(default[k], v, f"{path}.{k}" if path else k)
                elif not free:
                    raise ValueError(f"Unknown setting '{path + '.' if path else ''}{k}'")
    else:
        ok = True
    if not ok:
        raise ValueError(f"Setting '{path}' has the wrong type")


def validate_settings(patch: dict) -> None:
    if not isinstance(patch, dict):
        raise ValueError("Settings must be an object")
    _check(DEFAULT_SETTINGS, patch, "")
    tz = patch.get("timezone")
    if tz:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(tz)
        except Exception:
            raise ValueError(f"Unknown timezone '{tz}'")
    if "job_types" in patch:
        validate_job_types(patch["job_types"])
    group_by = (patch.get("areas") or {}).get("group_by")
    if group_by is not None and group_by not in GROUP_BY:
        raise ValueError(f"Area grouping must be one of: {', '.join(GROUP_BY)}")
    sched = patch.get("scheduling") or {}
    for key, (lo, hi) in SCHEDULING_LIMITS.items():
        if key in sched and not lo <= sched[key] <= hi:
            raise ValueError(f"scheduling.{key} must be between {lo} and {hi}")
    for prio, minutes in (sched.get("day_penalty_minutes") or {}).items():
        if minutes < 0:
            raise ValueError(f"The delay penalty for {prio} cannot be negative")
    if "urgency_keywords" in patch:
        words = patch["urgency_keywords"]
        if len(words) > 200 or not all(isinstance(w, str) and 0 < len(w.strip()) <= 80 for w in words):
            raise ValueError("Urgency keywords must be up to 200 words or phrases of 80 characters or fewer")
    map_cfg = patch.get("map") or {}
    tile = map_cfg.get("tile_url")
    if tile and not str(tile).startswith("https://"):
        raise ValueError("Map tile URL must start with https://")
    if "center" in map_cfg:
        c = map_cfg["center"]
        if not (len(c) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in c)
                and -90 <= c[0] <= 90 and -180 <= c[1] <= 180):
            raise ValueError("Map center must be [latitude, longitude]")
    if "zoom" in map_cfg and not 3 <= map_cfg["zoom"] <= 18:
        raise ValueError("Map zoom must be between 3 and 18")
    for company, rules in (patch.get("deadline_rules") or {}).items():
        if not isinstance(rules, dict):
            raise ValueError(f"Deadline rules for {company} must be an object of priority -> hours")
        for prio, hours in rules.items():
            if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0:
                raise ValueError(f"Deadline hours for {company}/{prio} must be a positive number")
    for alias, trade in (patch.get("trade_aliases") or {}).items():
        if not isinstance(trade, str) or not trade.strip():
            raise ValueError(f"Trade alias '{alias}' must map to a trade code")


def upgrade_stored(stored: dict) -> dict:
    """Settings saved by earlier versions, brought up to date so the Settings page keeps working and the admin's numbers
    survive. Earlier versions ranked warranty calls Emergency / Expedited / Normal by source (AHS, other warranty) and
    called everything else a "direct lead"; there are now Expedited / Normal / Recall warranty calls and Retail."""
    s = copy.deepcopy(stored or {})
    sc = s.get("scoring")
    if isinstance(sc, dict):
        if "base_direct_lead" in sc:
            sc.setdefault("base_retail", sc["base_direct_lead"])
        for old in ("base_direct_lead", "base_other_warranty"):
            sc.pop(old, None)
        if isinstance(sc.get("base_by_priority"), dict):
            sc["base_by_priority"].pop("Emergency", None)
    pen = (s.get("scheduling") or {}).get("day_penalty_minutes")
    if isinstance(pen, dict):
        if "Direct" in pen:
            pen.setdefault("Retail", pen["Direct"])
        pen.pop("Direct", None)
        pen.pop("Emergency", None)
    rules = s.get("deadline_rules")
    if isinstance(rules, dict) and ({"AHS", "OTHER_WARRANTY", "DIRECT"} & set(rules)) and not ({"WARRANTY", "RETAIL"} & set(rules)):
        warranty = {}
        for company in ("AHS", "OTHER_WARRANTY"):
            for prio, hours in (rules.get(company) or {}).items():
                if prio in ("Expedited", "Normal", "Recall"):
                    warranty.setdefault(prio, hours)
        direct = (rules.get("DIRECT") or {}).get("Normal")
        s["deadline_rules"] = deep_merge(DEFAULT_SETTINGS["deadline_rules"],
                                         {"WARRANTY": warranty, "RETAIL": {"Retail": direct} if direct else {}})
    return s


def get_settings(conn) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    stored = upgrade_stored(jload(row["value"], {}) if row else {})
    merged = deep_merge(DEFAULT_SETTINGS, stored)
    for key in FREE_FORM_MAPS:
        if key in stored:
            merged[key] = stored[key]
    return merged


def save_settings(conn, patch: dict) -> dict:
    validate_settings(patch)
    current = get_settings(conn)
    merged = deep_merge(current, patch)
    # replace (not merge) the free-form maps so removed companies/aliases actually disappear
    for key in FREE_FORM_MAPS:
        if key in patch:
            merged[key] = patch[key]
    validate_job_types(merged["job_types"])          # e.g. one tag under two types, checked across old and new values
    conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (SETTINGS_KEY, jdump(merged)))
    return merged


def seed_durations(conn) -> None:
    if conn.execute("SELECT COUNT(*) FROM job_durations").fetchone()[0] == 0:
        conn.executemany("INSERT OR IGNORE INTO job_durations(trade_code, keyword, minutes) VALUES (?,?,?)",
                         DEFAULT_DURATIONS)


def get_durations(conn) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT trade_code, keyword, minutes FROM job_durations ORDER BY trade_code, keyword")]


def replace_durations(conn, items: list) -> list:
    clean = []
    for it in items:
        if not isinstance(it, dict):
            raise ValueError("Each duration needs trade_code, keyword and minutes")
        trade = str(it.get("trade_code") or "*").strip().upper() or "*"
        kw = str(it.get("keyword") or "").strip().lower()
        try:
            mins = int(it.get("minutes"))
        except (TypeError, ValueError):
            raise ValueError("Duration minutes must be a whole number between 5 and 720") from None
        if not 5 <= mins <= 12 * 60:
            raise ValueError("Duration minutes must be between 5 and 720")
        clean.append((trade, kw, mins))
    conn.execute("DELETE FROM job_durations")
    conn.executemany("INSERT OR REPLACE INTO job_durations(trade_code, keyword, minutes) VALUES (?,?,?)", clean)
    return get_durations(conn)
