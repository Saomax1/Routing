"""
Admin-editable settings (stored as one JSON document in the ``settings`` table), with defaults.

IMPORTANT: ``deadline_rules`` are PLACEHOLDERS. They control the deadline warnings and the
urgency points in the priority score. Replace them with the real contact/schedule deadlines from
your warranty vendor agreements (Admin > Settings) before relying on the warning colors.
"""

from __future__ import annotations

import copy
from typing import Any

from ..db import jdump, jload
from ..domain.durations import DEFAULT_DURATIONS

SETTINGS_KEY = "app"

DEFAULT_SETTINGS: dict = {
    "timezone": "America/Phoenix",
    "scoring": {
        "base_by_priority": {"Emergency": 100, "Expedited": 60, "Normal": 20},
        "base_direct_lead": 30,
        "base_other_warranty": 20,
        "urgency_points_each": 10,
        "urgency_points_cap": 30,
        "age_points_per_day": 3,
        "age_points_cap": 30,
        "deadline_points": {"overdue": 40, "critical": 30, "warning": 15},
    },
    # Hours from "received" until the job should be scheduled/contacted. PLACEHOLDERS - confirm!
    "deadline_rules": {
        "AHS": {"Emergency": 4, "Expedited": 24, "Normal": 72},
        "OTHER_WARRANTY": {"Normal": 72},
        "DIRECT": {"Normal": 24},
    },
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
        # Extra "cost" (in minutes of driving) per day of delay, by priority: Emergency jobs prefer today
        "day_penalty_minutes": {"Emergency": 240, "Expedited": 90, "Normal": 15, "Direct": 10},
        "deadline_miss_penalty": 300,
        "travel_speed_mph": 28,
        "travel_circuity": 1.3,     # straight-line distance x this ~= road distance
        "min_travel_minutes": 3,
        "same_day_lead_minutes": 30,  # don't suggest a start sooner than this from now
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
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
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
    tile = (patch.get("map") or {}).get("tile_url")
    if tile and not str(tile).startswith("https://"):
        raise ValueError("Map tile URL must start with https://")
    for company, rules in (patch.get("deadline_rules") or {}).items():
        if not isinstance(rules, dict):
            raise ValueError(f"Deadline rules for {company} must be an object of priority -> hours")
        for prio, hours in rules.items():
            if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0:
                raise ValueError(f"Deadline hours for {company}/{prio} must be a positive number")
    for alias, trade in (patch.get("trade_aliases") or {}).items():
        if not isinstance(trade, str) or not trade.strip():
            raise ValueError(f"Trade alias '{alias}' must map to a trade code")


def get_settings(conn) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    stored = jload(row["value"], {}) if row else {}
    return deep_merge(DEFAULT_SETTINGS, stored)


def save_settings(conn, patch: dict) -> dict:
    validate_settings(patch)
    current = get_settings(conn)
    merged = deep_merge(current, patch)
    # replace (not merge) the free-form maps so removed companies/aliases actually disappear
    for key in ("deadline_rules", "trade_aliases"):
        if key in patch:
            merged[key] = patch[key]
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
        trade = str(it.get("trade_code") or "*").strip().upper() or "*"
        kw = str(it.get("keyword") or "").strip().lower()
        mins = int(it.get("minutes"))
        if not 5 <= mins <= 12 * 60:
            raise ValueError("Duration minutes must be between 5 and 720")
        clean.append((trade, kw, mins))
    conn.execute("DELETE FROM job_durations")
    conn.executemany("INSERT OR REPLACE INTO job_durations(trade_code, keyword, minutes) VALUES (?,?,?)", clean)
    return get_durations(conn)
