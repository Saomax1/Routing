#!/usr/bin/env python3
"""
Test routing on your REAL Housecall Pro data (read-only). Run it after  python scripts/go_live.py.

    python scripts/live_check.py                # sync, then check data, slot finder and drive times
    python scripts/live_check.py --no-sync      # use what the app has already loaded
    python scripts/live_check.py --jobs 10      # test the slot finder on 10 jobs (default 5)
    python scripts/live_check.py --mock         # rehearse the script on the demo data in a temporary database

It writes data/live_check_report.json: counts, timings and ratios only - no customer names, addresses, phone numbers
or job ids - so it is safe to share if something needs fixing. Housecall Pro is only ever read.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import load_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.hcp.client import MockHCPClient, make_hcp_client  # noqa: E402
from app.services.geocode import MockGeocoder, make_geocoder  # noqa: E402
from app.services.live_check import run_live_check  # noqa: E402
from app.services.routing import RoadRoutes, make_road_routes  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-sync", action="store_true")
    ap.add_argument("--jobs", type=int, default=5)
    ap.add_argument("--days", type=int, default=None, help="how many days ahead the slot finder looks")
    ap.add_argument("--legs", type=int, default=12, help="how many drive legs to compare with real roads")
    ap.add_argument("--mock", action="store_true", help="rehearse on the demo data in a temporary database")
    ap.add_argument("--out", default=os.path.join("data", "live_check_report.json"))
    args = ap.parse_args()

    cfg = load_config()
    if args.mock:
        from app.hcp.fixtures import DEMO_TECH_SETUP
        from app.services.sync import SyncService
        cfg.database_path = os.path.join(tempfile.mkdtemp(), "rehearsal.db")
        cfg.hcp_mode = "mock"
        db = Database(cfg.database_path)
        # demo technicians come pre-configured, like a finished Admin > Technicians setup
        SyncService(db, cfg, MockHCPClient(), MockGeocoder(), new_tech_defaults=lambda e: DEMO_TECH_SETUP.get(e)).run()
        hcp, geocoder, routes, do_sync = MockHCPClient(), MockGeocoder(), RoadRoutes(None), False
    else:
        if cfg.hcp_mode != "live" or not cfg.hcp_api_key:
            sys.exit("The app is not in live mode yet. Run  python scripts/go_live.py  first (or use --mock to rehearse).")
        db = Database(cfg.database_path)
        hcp, geocoder, routes, do_sync = make_hcp_client(cfg), make_geocoder(cfg), make_road_routes(cfg), not args.no_sync
    print(f"Mode: {'REHEARSAL on demo data' if args.mock else 'LIVE (read-only)'}  |  geocoder: {cfg.geocoder}  |  road routes: {getattr(routes, 'name', 'none')}")
    report = run_live_check(db, cfg, hcp, geocoder, routes, do_sync=do_sync, jobs=args.jobs, days=args.days, legs=args.legs)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport (counts and timings only, safe to share) written to {args.out}")
    sys.exit(1 if report.get("overall") == "FAIL" else 0)


if __name__ == "__main__":
    main()
