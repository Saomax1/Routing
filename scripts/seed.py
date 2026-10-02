#!/usr/bin/env python3
"""
Load the sanitized demo data into a fresh local database (spec deliverable: "seed script").

    python scripts/seed.py            # creates data/routing.db, a demo admin, and runs one mock sync

Always uses the mock Housecall Pro client + mock geocoder, regardless of .env, so it can never touch
real data. (The server also does this automatically on first start in mock mode.)
"""

import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import load_config  # noqa: E402
from app.db import Database, utcnow_iso  # noqa: E402
from app.hcp.client import MockHCPClient  # noqa: E402
from app.hcp.fixtures import DEMO_TECH_SETUP  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services.geocode import MockGeocoder  # noqa: E402
from app.services.sync import SyncService  # noqa: E402


def main():
    cfg = load_config()
    cfg.hcp_mode = "mock"
    db = Database(cfg.database_path)
    svc = SyncService(db, cfg, MockHCPClient(), MockGeocoder(), new_tech_defaults=lambda eid: DEMO_TECH_SETUP.get(eid))
    print("sync:", svc.run())
    with db.session() as conn:
        if not conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            email = cfg.bootstrap_admin_email or "admin@example.com"
            pw = cfg.bootstrap_admin_password or secrets.token_urlsafe(12)
            conn.execute("INSERT INTO users(email, name, password_hash, role, created_at) VALUES (?,?,?,?,?)",
                         (email.lower(), "Admin", hash_password(pw), "admin", utcnow_iso()))
            print(f"admin created: {email} / {pw}   (shown once)")
    print(f"database: {cfg.database_path}")


if __name__ == "__main__":
    main()
