#!/usr/bin/env python3
"""Create (or reset the password of) a login. Usage: python scripts/create_user.py"""

import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import load_config  # noqa: E402
from app.db import Database, utcnow_iso  # noqa: E402
from app.security import MIN_PASSWORD_LENGTH, ROLES, hash_password  # noqa: E402


def main():
    cfg = load_config()
    db = Database(cfg.database_path)
    email = input("Email: ").strip().lower()
    name = input("Name (optional): ").strip()
    role = input(f"Role {ROLES} [dispatcher]: ").strip().lower() or "dispatcher"
    if role not in ROLES:
        sys.exit(f"Role must be one of {ROLES}")
    pw = getpass.getpass(f"Password (min {MIN_PASSWORD_LENGTH} chars): ")
    if pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match")
    try:
        h = hash_password(pw)
    except ValueError as e:
        sys.exit(str(e))
    with db.session() as conn:
        conn.execute(
            "INSERT INTO users(email, name, password_hash, role, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(email) DO UPDATE SET password_hash=excluded.password_hash, role=excluded.role, "
            "name=CASE WHEN excluded.name != '' THEN excluded.name ELSE users.name END",
            (email, name, h, role, utcnow_iso()))
    print(f"Saved {role} account for {email}")


if __name__ == "__main__":
    main()
