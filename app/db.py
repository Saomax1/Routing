"""
SQLite persistence (stdlib sqlite3, no ORM).

The spec allows SQLite for a first version. Every table here maps 1:1 to the build spec's
data model (section 6) so it can move to Postgres later; queries are plain SQL.

Use short-lived connections: ``with db.session() as conn: ...`` (commits on success).
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL DEFAULT '',
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin','dispatcher')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS technicians (
  hcp_employee_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  trade_skills TEXT NOT NULL DEFAULT '[]',        -- JSON array, e.g. ["PLB","HVAC"]
  home_address TEXT NOT NULL DEFAULT '',
  home_lat REAL, home_lng REAL,
  shift_start TEXT NOT NULL DEFAULT '08:00',
  shift_end TEXT NOT NULL DEFAULT '17:00',
  work_days TEXT NOT NULL DEFAULT '[0,1,2,3,4]',  -- JSON array, Monday=0
  max_jobs_per_day INTEGER NOT NULL DEFAULT 6,
  color TEXT NOT NULL DEFAULT '#2563eb',
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  hcp_job_id TEXT PRIMARY KEY,
  work_status TEXT NOT NULL DEFAULT 'unscheduled',
  active INTEGER NOT NULL DEFAULT 1,              -- 0 = no longer returned by HCP for our sync window
  scheduled_start TEXT, scheduled_end TEXT,
  arrival_window_minutes INTEGER,
  assigned_employee_ids TEXT NOT NULL DEFAULT '[]',
  customer_name TEXT NOT NULL DEFAULT '',
  customer_phone TEXT NOT NULL DEFAULT '',
  street TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT '', zip TEXT NOT NULL DEFAULT '',
  lat REAL, lng REAL,
  geocode_status TEXT NOT NULL DEFAULT 'pending', -- pending | ok | failed | from_hcp
  lead_source TEXT NOT NULL DEFAULT '',
  job_type TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]',
  trade_code TEXT NOT NULL DEFAULT '',            -- canonical: PLB / HVAC / ELEC / APPL / ''
  source_category TEXT NOT NULL DEFAULT 'direct', -- ahs | other_warranty | direct
  description_raw TEXT NOT NULL DEFAULT '',
  description_hash TEXT NOT NULL DEFAULT '',
  hcp_created_at TEXT, hcp_updated_at TEXT, last_synced_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(work_status, active);
CREATE INDEX IF NOT EXISTS idx_jobs_sched ON jobs(scheduled_start);

CREATE TABLE IF NOT EXISTS warranty_details (
  hcp_job_id TEXT PRIMARY KEY REFERENCES jobs(hcp_job_id) ON DELETE CASCADE,
  warranty_company TEXT, dispatch_number TEXT, trade_code TEXT,
  dispatch_priority TEXT, priority_rank INTEGER NOT NULL DEFAULT 0,
  authorization_required INTEGER,
  do_not_collect_service_fee INTEGER NOT NULL DEFAULT 0,
  completion_date_required INTEGER NOT NULL DEFAULT 0,
  completion_reported INTEGER NOT NULL DEFAULT 0,   -- used by the Phase 2 compliance tracker
  parsed_by TEXT NOT NULL DEFAULT 'regex',          -- regex | ai
  parse_warnings TEXT NOT NULL DEFAULT '[]',
  reviewed INTEGER NOT NULL DEFAULT 0,
  data TEXT NOT NULL DEFAULT '{}',                  -- full parsed record (JSON)
  parsed_at TEXT
);

CREATE TABLE IF NOT EXISTS job_durations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_code TEXT NOT NULL DEFAULT '*',             -- '*' = any trade
  keyword TEXT NOT NULL DEFAULT '',                 -- '' = trade default
  minutes INTEGER NOT NULL,
  UNIQUE (trade_code, keyword)
);

CREATE TABLE IF NOT EXISTS schedule_actions (       -- audit log (written by Phase 2 write-back)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, user_id INTEGER, hcp_job_id TEXT NOT NULL,
  old_start TEXT, old_tech TEXT, new_start TEXT, new_tech TEXT,
  success INTEGER NOT NULL DEFAULT 0, response TEXT
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS geocode_cache (
  address_key TEXT PRIMARY KEY,
  lat REAL, lng REAL,
  status TEXT NOT NULL,                             -- ok | failed
  provider TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',           -- running | ok | error
  mode TEXT, jobs_seen INTEGER DEFAULT 0, jobs_changed INTEGER DEFAULT 0,
  error TEXT
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def jdump(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def jload(s: Any, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init(self) -> None:
        conn = self._connect()
        try:
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def session(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def rows(cur) -> list:
    return [dict(r) for r in cur.fetchall()]
