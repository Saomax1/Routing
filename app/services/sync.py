"""
Housecall Pro -> local database sync (spec 4.3).

Every run:
  1. pulls employees, ALL unscheduled jobs, scheduled / in-progress jobs for today .. today+window days, and
     jobs HCP has marked COMPLETE over the last few days (they stay on the map as done, work_status = 'complete')
  2. for each job: normalise, parse warranty text (only if the description changed), geocode
     (cached; only if the address changed), upsert
  3. marks open jobs HCP no longer returns (inside our window) as inactive so they leave the map
  4. forgets bookings made in this app that HCP now shows as scheduled (see services/bookings.py)

Design notes
* Read-only against HCP in this phase.
* One bad record never aborts the run: each job is processed inside a savepoint and counted as an error.
* Commits after every job so a slow geocode call never holds the SQLite write lock.
* Nothing in here logs descriptions, names or phone numbers - only counts and ids.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from ..config import Config
from ..db import Database, jdump, jload, utcnow_iso
from ..domain.timeutil import at_local_minutes, to_iso
from ..domain.warranty_parser import looks_like_warranty, missing_key_fields, parse_warranty_job
from ..hcp.normalize import (canonical_trade, classify_source, guess_trade_from_text, normalize_employee,
                             normalize_job)
from .ai_fallback import ai_fill
from .bookings import drop_stale
from .data_mode import has_real_data, purge_demo_data
from .geocode import geocode_cached
from .settings_store import get_settings, seed_durations

log = logging.getLogger("routing.sync")

TECH_COLORS = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#be185d", "#65a30d",
               "#4f46e5", "#b45309"]


def _addr_str(street: str, city: str, state: str, zip_code: str) -> str:
    tail = " ".join(x for x in (state, zip_code) if x)
    return ", ".join(x for x in (street, city, tail) if x)


class SyncService:
    def __init__(self, db: Database, cfg: Config, hcp, geocoder, ai_transport=None,
                 new_tech_defaults: Optional[Callable[[str], dict]] = None):
        self.db, self.cfg, self.hcp, self.geocoder = db, cfg, hcp, geocoder
        self.ai_transport, self.new_tech_defaults = ai_transport, new_tech_defaults
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public
    def run(self, now: Optional[datetime] = None) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"status": "busy"}
        try:
            return self._run(now or datetime.now(timezone.utc))
        finally:
            self._lock.release()

    # ----------------------------------------------------------------- internals
    def _run(self, now: datetime) -> dict:
        with self.db.session() as conn:
            seed_durations(conn)
            settings = get_settings(conn)
            run_id = conn.execute("INSERT INTO sync_runs(started_at, status, mode) VALUES (?, 'running', ?)",
                                  (utcnow_iso(), self.cfg.hcp_mode)).lastrowid
            refusal = self._keep_demo_and_real_apart(conn)
        if refusal:
            self._finish(run_id, "error", 0, 0, refusal)
            return {"status": "error", "error": refusal}
        tz = ZoneInfo(settings["timezone"])
        today = now.astimezone(tz).date()
        end = today + timedelta(days=self.cfg.scheduled_window_days)

        try:
            employees = self.hcp.list_employees()
            unscheduled = self.hcp.list_unscheduled()
            scheduled = self.hcp.list_scheduled(today, end, tz)
        except Exception as e:  # network / auth / plan problems: record and stop; keep old data
            msg = f"{type(e).__name__}: {e}"[:300]
            log.error("sync failed fetching from HCP: %s", msg)
            self._finish(run_id, "error", 0, 0, msg)
            return {"status": "error", "error": msg}
        # Completed jobs are a separate call so a problem with it (the HCP status names are unverified) never
        # costs us the rest of the sync: jobs just stay as they were until the call works.
        completed, completed_error = [], None
        try:
            completed = self.hcp.list_completed(today - timedelta(days=self.cfg.completed_lookback_days), today, tz)
        except Exception as e:
            completed_error = type(e).__name__
            log.warning("sync: could not fetch completed jobs (%s)", completed_error)

        window_lo = to_iso(at_local_minutes(today, 0, tz))
        window_hi = to_iso(at_local_minutes(end + timedelta(days=1), 0, tz))
        seen, changed, errors = set(), 0, 0

        conn = self.db._connect()
        try:
            self._sync_employees(conn, employees)
            conn.commit()
            for raw in list(unscheduled) + list(scheduled) + list(completed):
                try:
                    n = normalize_job(raw)
                    if not n["hcp_job_id"]:
                        continue
                    conn.execute("SAVEPOINT job")
                    changed += 1 if self._upsert_job(conn, n, settings, now) else 0
                    conn.execute("RELEASE job")
                    conn.commit()
                    seen.add(n["hcp_job_id"])
                except Exception as e:
                    errors += 1
                    log.warning("sync: skipped a job (%s)", type(e).__name__)
                    try:
                        conn.execute("ROLLBACK TO job")
                        conn.execute("RELEASE job")
                        conn.commit()
                    except Exception:
                        conn.rollback()
            # deactivate what HCP no longer returns inside our window (completed jobs are history: they stay)
            for r in conn.execute("SELECT hcp_job_id, work_status, scheduled_start FROM jobs WHERE active = 1").fetchall():
                if r["hcp_job_id"] in seen or r["work_status"] == "complete":
                    continue
                in_window = r["work_status"] == "unscheduled" or (
                    r["scheduled_start"] and window_lo <= r["scheduled_start"] < window_hi)
                if in_window:
                    conn.execute("UPDATE jobs SET active = 0 WHERE hcp_job_id = ?", (r["hcp_job_id"],))
                    changed += 1
            drop_stale(conn, now)          # bookings HCP has caught up with, or whose window has ended
            conn.commit()
        finally:
            conn.close()

        status = "ok" if errors == 0 else "partial"
        note = []
        if errors:
            note.append(f"{errors} job(s) could not be processed")
        if completed_error:
            note.append(f"completed jobs could not be fetched ({completed_error})")
        if getattr(self.hcp, "truncated", False):
            note.append("HCP result list was truncated at the page cap")
        self._finish(run_id, status, len(seen), changed, "; ".join(note) or None)
        log.info("sync %s: %d jobs seen, %d changed, %d errors", status, len(seen), changed, errors)
        return {"status": status, "jobs_seen": len(seen), "jobs_changed": changed, "errors": errors}

    def _keep_demo_and_real_apart(self, conn) -> Optional[str]:
        """Live mode: demo rows are removed. Demo mode: refuse to add demo data to a database holding real data."""
        if self.cfg.hcp_mode == "live":
            gone = purge_demo_data(conn)
            if any(gone.values()):
                log.warning("sync: removed demo data (%d jobs, %d technicians) before loading Housecall Pro data",
                            gone["jobs"], gone["technicians"])
            return None
        if has_real_data(conn):
            return ("This database holds real Housecall Pro data, so the built-in demo data will not be loaded into it "
                    "(it would put fake jobs next to real customers). Set HCP_MODE=live, or point DATABASE_PATH at a "
                    "different file for demos.")
        return None

    def _finish(self, run_id: int, status: str, seen: int, changed: int, error: Optional[str]) -> None:
        with self.db.session() as conn:
            conn.execute("UPDATE sync_runs SET finished_at=?, status=?, jobs_seen=?, jobs_changed=?, error=? WHERE id=?",
                         (utcnow_iso(), status, seen, changed, error, run_id))

    # ---------------------------------------------------------------- technicians
    def _sync_employees(self, conn, employees: list) -> None:
        for raw in employees:
            e = normalize_employee(raw)
            if not e["hcp_employee_id"]:
                continue
            row = conn.execute("SELECT hcp_employee_id FROM technicians WHERE hcp_employee_id = ?",
                               (e["hcp_employee_id"],)).fetchone()
            if row:
                conn.execute("UPDATE technicians SET name = ? WHERE hcp_employee_id = ?", (e["name"], e["hcp_employee_id"]))
                if not e["active"]:  # HCP deactivated them; never auto-reactivate (admin may have turned routing off)
                    conn.execute("UPDATE technicians SET active = 0 WHERE hcp_employee_id = ?", (e["hcp_employee_id"],))
                continue
            defaults = (self.new_tech_defaults(e["hcp_employee_id"]) if self.new_tech_defaults else None) or {}
            count = conn.execute("SELECT COUNT(*) FROM technicians").fetchone()[0]
            home = defaults.get("home_address", "")
            lat = lng = None
            if home:
                lat, lng, _ = geocode_cached(conn, self.geocoder, home)
            # Live accounts: new employees start with routing OFF until an admin sets skills + home base
            # (this also keeps office staff off the map). Demo employees are pre-configured.
            enabled = 1 if (defaults and e["active"]) else 0
            conn.execute(
                "INSERT INTO technicians(hcp_employee_id, name, active, trade_skills, home_address, home_lat, home_lng, "
                "color, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (e["hcp_employee_id"], e["name"], enabled, jdump(defaults.get("trade_skills", [])), home, lat, lng,
                 TECH_COLORS[count % len(TECH_COLORS)], utcnow_iso()))

    # ----------------------------------------------------------------------- jobs
    def _upsert_job(self, conn, n: dict, settings: dict, now: datetime) -> bool:
        """Insert/update one job. Returns True if anything meaningful changed."""
        jid = n["hcp_job_id"]
        existing = conn.execute("SELECT * FROM jobs WHERE hcp_job_id = ?", (jid,)).fetchone()
        desc = n["description_raw"]
        dhash = hashlib.sha256(desc.encode("utf-8")).hexdigest()
        need_parse = existing is None or existing["description_hash"] != dhash

        # --- warranty parse (only when text changed)
        wdata, parsed_by, warnings = None, "regex", []
        if need_parse:
            if looks_like_warranty(desc):
                w = parse_warranty_job(desc)
                if self.cfg.llm_enabled and missing_key_fields(w):
                    ai_fill(self.cfg, desc, w, self.ai_transport)
                wdata, parsed_by, warnings = asdict(w), w.parsed_by, list(w.parse_warnings)
        else:
            wrow = conn.execute("SELECT data, parsed_by, parse_warnings FROM warranty_details WHERE hcp_job_id = ?",
                                (jid,)).fetchone()
            if wrow:
                wdata, parsed_by, warnings = jload(wrow["data"], {}), wrow["parsed_by"], jload(wrow["parse_warnings"], [])

        aliases = settings["trade_aliases"]
        company = (wdata or {}).get("warranty_company") if (wdata and wdata.get("is_warranty")) else None
        trade = (canonical_trade((wdata or {}).get("trade_code") or "", aliases)
                 or canonical_trade(n["job_type"], aliases)
                 or guess_trade_from_text(desc[:800]))
        category = classify_source(n["lead_source"], n["tags"], company)

        # --- address: the warranty "Covered Property Address" wins (it is the address to geocode)
        street, city, state, zip_code = n["street"], n["city"], n["state"], n["zip"]
        if wdata and wdata.get("full_address"):
            street, city, state, zip_code = wdata["street"], wdata["city"], wdata["state"], wdata["zip_code"]
        new_addr = _addr_str(street, city, state, zip_code)

        lat = lng = None
        geo_status = "failed" if not new_addr and n["hcp_lat"] is None else "pending"
        old_addr = _addr_str(existing["street"], existing["city"], existing["state"], existing["zip"]) if existing else ""
        if existing and existing["geocode_status"] in ("ok", "from_hcp") and old_addr == new_addr \
                and existing["lat"] is not None:
            lat, lng, geo_status = existing["lat"], existing["lng"], existing["geocode_status"]
        elif n["hcp_lat"] is not None and not (wdata and wdata.get("full_address")):
            lat, lng, geo_status = n["hcp_lat"], n["hcp_lng"], "from_hcp"
        elif new_addr:
            lat, lng, st = geocode_cached(conn, self.geocoder, new_addr, now)
            geo_status = {"ok": "ok", "failed": "failed", "error": "pending"}[st]

        row = {
            "hcp_job_id": jid, "work_status": n["work_status"], "active": 1,
            "scheduled_start": n["scheduled_start"], "scheduled_end": n["scheduled_end"],
            "arrival_window_minutes": n["arrival_window_minutes"],
            # when HCP does not say, a completed job's last update / scheduled end is the best guess
            "completed_at": (n["completed_at"] or n["hcp_updated_at"] or n["scheduled_end"])
            if n["work_status"] == "complete" else None,
            "assigned_employee_ids": jdump(n["assigned_employee_ids"]),
            "customer_name": n["customer_name"] or (wdata or {}).get("contact_name") or "",
            "customer_phone": n["customer_phone"] or (((wdata or {}).get("contact_phones") or [""])[0]),
            "street": street, "city": city, "state": state, "zip": zip_code,
            "lat": lat, "lng": lng, "geocode_status": geo_status,
            "lead_source": n["lead_source"], "job_type": n["job_type"], "tags": jdump(n["tags"]),
            "trade_code": trade, "source_category": category,
            "description_raw": desc, "description_hash": dhash,
            "hcp_created_at": n["hcp_created_at"], "hcp_updated_at": n["hcp_updated_at"],
            "last_synced_at": utcnow_iso(),
        }
        compare = ("work_status", "active", "scheduled_start", "scheduled_end", "arrival_window_minutes",
                   "completed_at", "assigned_employee_ids", "street", "city",
                   "zip", "lat", "lng", "lead_source", "tags", "description_hash", "hcp_updated_at", "trade_code",
                   "source_category", "customer_name")
        changed = existing is None or any(existing[k] != row[k] for k in compare)

        cols = list(row)
        conn.execute(
            f"INSERT INTO jobs({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(hcp_job_id) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols if c != 'hcp_job_id')}",
            [row[c] for c in cols])

        if need_parse:
            if wdata:
                conn.execute(
                    "INSERT INTO warranty_details(hcp_job_id, warranty_company, dispatch_number, trade_code, dispatch_priority, "
                    "priority_rank, authorization_required, do_not_collect_service_fee, completion_date_required, parsed_by, "
                    "parse_warnings, reviewed, data, parsed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?) "
                    "ON CONFLICT(hcp_job_id) DO UPDATE SET warranty_company=excluded.warranty_company, "
                    "dispatch_number=excluded.dispatch_number, trade_code=excluded.trade_code, "
                    "dispatch_priority=excluded.dispatch_priority, priority_rank=excluded.priority_rank, "
                    "authorization_required=excluded.authorization_required, "
                    "do_not_collect_service_fee=excluded.do_not_collect_service_fee, "
                    "completion_date_required=excluded.completion_date_required, parsed_by=excluded.parsed_by, "
                    "parse_warnings=excluded.parse_warnings, reviewed=0, data=excluded.data, parsed_at=excluded.parsed_at",
                    (jid, wdata.get("warranty_company"), wdata.get("dispatch_number"), wdata.get("trade_code"),
                     wdata.get("dispatch_priority"), wdata.get("priority_rank") or 0,
                     None if wdata.get("authorization_required") is None else int(wdata["authorization_required"]),
                     int(bool(wdata.get("do_not_collect_service_fee"))), int(bool(wdata.get("completion_date_required"))),
                     parsed_by, jdump(warnings), jdump(wdata), utcnow_iso()))
            else:
                conn.execute("DELETE FROM warranty_details WHERE hcp_job_id = ?", (jid,))
        return changed
