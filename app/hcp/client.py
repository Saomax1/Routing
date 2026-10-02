"""
Housecall Pro clients.

``HCPClient``      - live calls (read-only in this phase; write-back arrives in Phase 2)
``MockHCPClient``  - serves sanitized demo data, no network (HCP_MODE=mock, the default)

Both expose the same read methods, returning RAW HCP-shaped dicts which
``normalize.normalize_job`` / ``normalize_employee`` then flatten:

    list_employees()                       -> [raw employee]
    list_unscheduled()                     -> [raw job]
    list_scheduled(start_date, end_date)   -> [raw job]   (scheduled + in progress)
    list_completed(start_date, end_date)   -> [raw job]   (complete rated / unrated)

!!  Endpoint paths, query-parameter names and the auth scheme are UNVERIFIED (see normalize.py).
!!  They are isolated in the constants below. ``scripts/phase0_probe.py`` exercises them against your
!!  real account and shows exactly what to change. API access requires an HCP plan that includes it
!!  (reported as the MAX plan) and an Admin user to generate the key.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any, List, Optional
from zoneinfo import ZoneInfo

from ..config import Config
from ..domain.timeutil import parse_iso, to_iso
from .fixtures import make_demo_dataset
from .http import UrllibTransport
from .normalize import canonical_work_status

log = logging.getLogger("routing.hcp")

# ---- UNVERIFIED API shape: adjust here after running the Phase 0 probe ----
JOBS_PATH = "/jobs"
EMPLOYEES_PATH = "/employees"
PARAM_PAGE = "page"
PARAM_PAGE_SIZE = "page_size"
PARAM_WORK_STATUS = "work_status[]"
STATUS_OPEN_SCHEDULED = ("scheduled", "in progress")
STATUS_COMPLETED = ("complete rated", "complete unrated")
PARAM_SCHED_MIN = "scheduled_start_min"
PARAM_SCHED_MAX = "scheduled_start_max"
LIST_KEYS = {"jobs": ("jobs", "data", "items", "results"), "employees": ("employees", "data", "items", "results")}
MAX_PAGES = 50  # safety cap: 50 pages x page_size jobs per sync


def _extract_list(payload: Any, kind: str) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in LIST_KEYS[kind]:
            if isinstance(payload.get(key), list):
                return payload[key]
        lists = [v for v in payload.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    return []


class HCPClient:
    def __init__(self, cfg: Config, transport: Optional[UrllibTransport] = None):
        if not cfg.hcp_api_key:
            raise ValueError("HCP_API_KEY is not set")
        self.base = cfg.hcp_base_url
        self.page_size = cfg.hcp_page_size
        self._key = cfg.hcp_api_key
        self._scheme = cfg.hcp_auth_scheme
        self.transport = transport or UrllibTransport()
        self.truncated = False   # set when MAX_PAGES was hit (so the admin page can warn)

    def __repr__(self) -> str:
        return f"HCPClient(base={self.base!r}, key=***)"

    def _headers(self) -> dict:
        return {"Authorization": f"{self._scheme} {self._key}", "Accept": "application/json"}

    def _paged(self, path: str, kind: str, params: Optional[list] = None) -> List[dict]:
        out: List[dict] = []
        for page in range(1, MAX_PAGES + 1):
            q = list(params or []) + [(PARAM_PAGE, page), (PARAM_PAGE_SIZE, self.page_size)]
            payload = self.transport.request("GET", self.base + path, headers=self._headers(), params=q)
            items = _extract_list(payload, kind)
            out.extend(items)
            total_pages = payload.get("total_pages") if isinstance(payload, dict) else None
            if not items or (total_pages is not None and page >= int(total_pages)) \
                    or (total_pages is None and len(items) < self.page_size):
                return out
        self.truncated = True
        log.warning("HCP pagination stopped at %d pages; some jobs may be missing", MAX_PAGES)
        return out

    # ---- reads ----
    def list_employees(self) -> List[dict]:
        return self._paged(EMPLOYEES_PATH, "employees")

    def list_unscheduled(self) -> List[dict]:
        return self._paged(JOBS_PATH, "jobs", [(PARAM_WORK_STATUS, "unscheduled")])

    def list_scheduled(self, start: date, end: date, tz: Optional[ZoneInfo] = None) -> List[dict]:
        tz = tz or ZoneInfo("UTC")
        lo = datetime.combine(start, time(0, 0), tzinfo=tz)
        hi = datetime.combine(end + timedelta(days=1), time(0, 0), tzinfo=tz)
        return self._paged(JOBS_PATH, "jobs", [(PARAM_WORK_STATUS, s) for s in STATUS_OPEN_SCHEDULED]
                           + [(PARAM_SCHED_MIN, to_iso(lo)), (PARAM_SCHED_MAX, to_iso(hi))])

    def list_completed(self, start: date, end: date, tz: Optional[ZoneInfo] = None) -> List[dict]:
        """Jobs HCP has marked complete whose scheduled start falls in [start, end]."""
        tz = tz or ZoneInfo("UTC")
        lo = datetime.combine(start, time(0, 0), tzinfo=tz)
        hi = datetime.combine(end + timedelta(days=1), time(0, 0), tzinfo=tz)
        return self._paged(JOBS_PATH, "jobs", [(PARAM_WORK_STATUS, s) for s in STATUS_COMPLETED]
                           + [(PARAM_SCHED_MIN, to_iso(lo)), (PARAM_SCHED_MAX, to_iso(hi))])

    # ---- writes: Phase 2 (explicit dispatcher action + confirmation only) ----
    def set_schedule(self, *a, **k):
        raise NotImplementedError("Write-back to Housecall Pro is Phase 2 and is not enabled.")

    def assign_employees(self, *a, **k):
        raise NotImplementedError("Write-back to Housecall Pro is Phase 2 and is not enabled.")


class MockHCPClient:
    """Serves the sanitized demo dataset. Same interface as HCPClient; never touches the network."""

    mode = "mock"

    def __init__(self, tz_name: str = "America/Phoenix", now: Optional[datetime] = None):
        self.dataset = make_demo_dataset(now=now, tz_name=tz_name)
        self.truncated = False

    def list_employees(self) -> List[dict]:
        return list(self.dataset["employees"])

    def list_unscheduled(self) -> List[dict]:
        return [j for j in self.dataset["jobs"] if j["work_status"] == "unscheduled"]

    def _in_range(self, statuses: tuple, start: date, end: date, tz: Optional[ZoneInfo]) -> List[dict]:
        tz = tz or ZoneInfo("America/Phoenix")
        out = []
        for j in self.dataset["jobs"]:
            if canonical_work_status(j["work_status"]) not in statuses:
                continue
            s = parse_iso((j.get("schedule") or {}).get("scheduled_start"))
            if s and start <= s.astimezone(tz).date() <= end:
                out.append(j)
        return out

    def list_scheduled(self, start: date, end: date, tz: Optional[ZoneInfo] = None) -> List[dict]:
        return self._in_range(("scheduled", "in_progress"), start, end, tz)

    def list_completed(self, start: date, end: date, tz: Optional[ZoneInfo] = None) -> List[dict]:
        return self._in_range(("complete",), start, end, tz)

    def set_schedule(self, *a, **k):
        raise NotImplementedError("Write-back is Phase 2.")

    assign_employees = set_schedule


def make_hcp_client(cfg: Config, tz_name: str = "America/Phoenix"):
    if cfg.hcp_mode == "live":
        return HCPClient(cfg)
    return MockHCPClient(tz_name=tz_name)
