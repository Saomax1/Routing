"""Estimate how long a job takes from (trade, problem text) using the admin-editable table."""

from __future__ import annotations

from typing import Iterable

from .warranty_parser import keyword_present

# Seed values (minutes). Editable on the Admin > Settings page; these are only starting guesses.
DEFAULT_DURATIONS = [
    ("*", "", 60),
    ("PLB", "", 60),
    ("PLB", "water heater", 120),
    ("PLB", "stoppage", 75),
    ("PLB", "leak", 90),
    ("PLB", "toilet", 60),
    ("PLB", "faucet", 60),
    ("HVAC", "", 75),
    ("HVAC", "no cooling", 90),
    ("HVAC", "no heat", 90),
    ("HVAC", "tune-up", 60),
    ("HVAC", "thermostat", 45),
]


def estimate_minutes(trade_code: str, text: str, rows: Iterable[dict], fallback: int = 60) -> int:
    """Longest matching keyword for the trade wins; then the trade default; then any-trade; then fallback.

    ``rows`` are dicts with trade_code / keyword / minutes (the job_durations table).
    """
    trade = (trade_code or "").upper()
    text = text or ""
    best_kw_len, best = -1, None
    trade_default = any_default = None
    for r in rows:
        rt, kw, mins = (r["trade_code"] or "*").upper(), (r["keyword"] or "").strip(), int(r["minutes"])
        if rt not in (trade, "*"):
            continue
        if kw == "":
            if rt == trade:
                trade_default = mins
            else:
                any_default = mins
        elif keyword_present(kw, text) and (len(kw) > best_kw_len or (len(kw) == best_kw_len and rt == trade)):
            best_kw_len, best = len(kw), mins
    for v in (best, trade_default, any_default):
        if v is not None:
            return v
    return fallback
