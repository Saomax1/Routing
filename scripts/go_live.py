#!/usr/bin/env python3
"""
Switch the app from the demo to your real Housecall Pro account - READ-ONLY.

    python scripts/go_live.py

What it does, step by step (you are asked before anything that needs a choice):
  1. asks for your Housecall Pro API key (typed hidden: it is not shown and never printed) and tests it with one
     read of the employee list;
  2. asks how addresses become map pins and whether to draw road routes (both send customer locations to that
     provider; the choices explain the trade-off);
  3. removes the fake demo customers, jobs and technicians from the database;
  4. offers to replace the demo login (admin@example.com) with your own;
  5. saves everything to .env (never committed to git).

The app only ever READS Housecall Pro: its connection to Housecall Pro is limited to GET requests in code. Whether
the key itself can write is decided by Housecall Pro, not by this app - create a read-only key there if it offers one.

Then start the app (python -m app) and run  python scripts/live_check.py  to test routing on your real data.
"""

from __future__ import annotations

import getpass
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Config, load_config  # noqa: E402
from app.db import Database, utcnow_iso  # noqa: E402
from app.security import MIN_PASSWORD_LENGTH, hash_password  # noqa: E402
from app.services.data_mode import purge_demo_data  # noqa: E402
from app.services.live_setup import check_key, only_the_demo_login_exists, replace_demo_login, update_env_file  # noqa: E402


class Console:
    """Questions and answers (swapped for a script in the tests)."""

    def say(self, text: str = "") -> None:
        print(text)

    def ask(self, prompt: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        return input(f"{prompt}{suffix}: ").strip() or default

    def secret(self, prompt: str) -> str:
        return getpass.getpass(f"{prompt} (hidden): ").strip()


def _choose(io, title: str, options: list, default: int = 1) -> int:
    io.say(f"\n{title}")
    for i, (label, _) in enumerate(options, 1):
        io.say(f"  {i}) {label}")
    while True:
        raw = io.ask("Choose", str(default))
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        io.say(f"Please type a number from 1 to {len(options)}.")


def _yes(io, prompt: str, default: bool = True) -> bool:
    return io.ask(f"{prompt} (y/n)", "y" if default else "n").lower().startswith("y")


def run(io=None, env_path: Path = ROOT / ".env", database_path: str = "", transport=None, existing_key: str = "",
        base_url: str = "") -> int:
    io = io or Console()
    cfg = load_config()
    base_url = base_url or cfg.hcp_base_url
    db_path = database_path or cfg.database_path

    io.say("Go live: connect the app to your real Housecall Pro account (read-only).")

    # ---- 1. the key
    key = existing_key
    if key and not _yes(io, "A Housecall Pro API key is already saved in .env. Keep using it?"):
        key = ""
    attempts = 0
    while True:
        if not key:
            key = io.secret("Housecall Pro API key")
        probe = Config(hcp_mode="live", hcp_api_key=key, hcp_base_url=base_url, hcp_auth_scheme=cfg.hcp_auth_scheme,
                       hcp_page_size=cfg.hcp_page_size)
        result = check_key(probe, transport) if key else {"ok": False, "message": "No key entered."}
        io.say(("OK: " if result["ok"] else "Problem: ") + result["message"])
        if result["ok"]:
            break
        attempts += 1
        if attempts >= 3 or not _yes(io, "Try a different key?"):
            io.say("Nothing was changed.")
            return 1
        key = ""

    # ---- 2. geocoder + routes
    values = {"HCP_MODE": "live", "HCP_API_KEY": key}
    maps_key = ""
    g = _choose(io, "How should customer addresses become map pins?  (each address is sent to the provider)", [
        ("US Census geocoder: free, no account, US addresses only (addresses go to the US Census Bureau)", "census"),
        ("Google Maps geocoding (needs a Google Maps API key)", "google"),
        ("Mapbox geocoding (needs a Mapbox token)", "mapbox")])
    geocoder = ["census", "google", "mapbox"][g]
    if geocoder != "census":
        maps_key = io.secret("Maps API key")
        if not maps_key:
            io.say("No maps key entered, so using the free US Census geocoder instead.")
            geocoder = "census"
    values["GEOCODER"] = geocoder

    r = _choose(io, "Draw road routes between jobs? Each stop's coordinates (a customer location) are sent to the provider.", [
        ("No: straight lines and estimated drive times (nothing is sent anywhere) - recommended to start", "none"),
        ("Mapbox Directions (needs a Mapbox token; commercial, private)", "mapbox"),
        ("My own OSRM server (you give its address)", "osrm"),
        ("The free public OSRM demo server (NOT recommended with real customers: shared, fair-use only, third party)", "public")])
    router = ["none", "mapbox", "osrm", "public"][r]
    if router == "mapbox":
        if geocoder == "google":          # the app has a single maps key, and a Google key does not work for Mapbox
            io.say("Google and Mapbox need different keys and this app keeps one maps key, so road routes stay off. "
                   "(Pick Mapbox for both, or the Census geocoder with Mapbox routes.)")
            router = "none"
        elif not maps_key:
            maps_key = io.secret("Mapbox token")
            if not maps_key:
                io.say("No token entered, so road routes stay off.")
                router = "none"
    if router == "osrm":
        url = io.ask("OSRM server address (https://...)")
        if url.startswith(("http://", "https://")):
            values["ROUTER_URL"] = url.rstrip("/")
        else:
            io.say("That is not a web address, so road routes stay off.")
            router = "none"
    values["ROUTER"] = "osrm" if router in ("osrm", "public") else router
    if router == "public":
        values["ROUTER_URL"] = "https://router.project-osrm.org"
    if maps_key:
        values["MAPS_API_KEY"] = maps_key
    if not os.environ.get("SESSION_SECRET"):
        values["SESSION_SECRET"] = secrets.token_urlsafe(48)       # stable logins across restarts

    # ---- 3. the fake data
    db = Database(db_path)
    with db.session() as conn:
        gone = purge_demo_data(conn)
        if any(gone.values()):
            io.say(f"\nRemoved the demo data: {gone['jobs']} fake jobs and {gone['technicians']} fake technicians.")
        else:
            io.say("\nNo demo data in the database.")

        # ---- 4. login
        if only_the_demo_login_exists(conn) and _yes(
                io, "\nThe demo login (admin@example.com) is still the only account. Create your own admin login now? "
                    "(the demo one is removed)"):
            email = io.ask("Your email").lower()
            name = io.ask("Your name", "Admin")
            password = io.secret(f"Password (at least {MIN_PASSWORD_LENGTH} characters)")
            if "@" in email and password and password == io.secret("Repeat the password") and len(password) >= MIN_PASSWORD_LENGTH:
                replace_demo_login(conn, email, name, hash_password(password), utcnow_iso())
                io.say(f"Login created for {email}; the demo login is gone.")
            else:
                io.say("That email or password was not usable (or they did not match), so the demo login stays for now. "
                       "Make your own with:  python scripts/create_user.py")

    # ---- 5. save
    update_env_file(env_path, values, template=ROOT / ".env.example")
    io.say(f"\nSaved to {env_path.name}: live mode, geocoder = {geocoder}, road routes = {router}. "
           "The API key is stored there and is not shown anywhere.")
    io.say("\nNext:")
    io.say("  1. python -m app                  (starts the app and loads your real jobs)")
    io.say("  2. Admin > Technicians            (set each technician's trade skills, home address and hours)")
    io.say("  3. python scripts/live_check.py   (tests routing on your real data and writes a shareable report)")
    return 0


if __name__ == "__main__":
    sys.exit(run(existing_key=os.environ.get("HCP_API_KEY", "")))
