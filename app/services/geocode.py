"""
Geocoding with a persistent cache (spec: "cached geocoding").

Providers (GEOCODER env var):
  mock    offline, deterministic city-center + jitter (demo/tests; NOT real addresses)
  census  US Census geocoder - free, no key, US addresses only
  google  Google Geocoding API (MAPS_API_KEY)
  mapbox  Mapbox Geocoding API (MAPS_API_KEY)

A successful lookup is cached forever (addresses don't move); a failed lookup is cached for 24h so
we neither hammer the provider nor retry bad addresses on every sync. Transient errors (network,
5xx) are NOT cached.
"""

from __future__ import annotations

import hashlib
import logging
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from ..config import Config
from ..db import utcnow_iso
from ..domain.timeutil import parse_iso
from ..hcp.fixtures import CITY_CENTERS
from ..hcp.http import HttpError, UrllibTransport

log = logging.getLogger("routing.geocode")

LatLng = Tuple[float, float]
FAIL_RETRY = timedelta(hours=24)


def address_key(address: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (address or "").lower())).strip()


class MockGeocoder:
    name = "mock"

    def geocode(self, address: str) -> Optional[LatLng]:
        a = (address or "").lower()
        center = None
        for city, (lat, lng, _) in CITY_CENTERS.items():
            if city.lower() in a:
                center = (lat, lng)
                break
        if center is None:
            m = re.search(r"\b(\d{5})\b", a)
            if m:
                for lat, lng, zips in CITY_CENTERS.values():
                    if m.group(1) in zips:
                        center = (lat, lng)
                        break
        if center is None:
            return None
        h = hashlib.md5(address_key(address).encode()).digest()
        jl = (int.from_bytes(h[:4], "big") / 2 ** 32) * 2 - 1
        jn = (int.from_bytes(h[4:8], "big") / 2 ** 32) * 2 - 1
        return (round(center[0] + jl * 0.022, 6), round(center[1] + jn * 0.028, 6))


class CensusGeocoder:
    name = "census"
    URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

    def __init__(self, transport: Optional[UrllibTransport] = None):
        self.t = transport or UrllibTransport(timeout=20, max_attempts=2)

    def geocode(self, address: str) -> Optional[LatLng]:
        data = self.t.request("GET", self.URL, params=[("address", address), ("benchmark", "Public_AR_Current"),
                                                       ("format", "json")])
        matches = ((data or {}).get("result") or {}).get("addressMatches") or []
        if not matches:
            return None
        c = matches[0]["coordinates"]
        return float(c["y"]), float(c["x"])


class GoogleGeocoder:
    name = "google"
    URL = "https://maps.googleapis.com/maps/api/geocode/json"

    def __init__(self, key: str, transport: Optional[UrllibTransport] = None):
        self.key, self.t = key, transport or UrllibTransport(timeout=20, max_attempts=3)

    def geocode(self, address: str) -> Optional[LatLng]:
        data = self.t.request("GET", self.URL, params=[("address", address), ("region", "us"), ("key", self.key)])
        status = (data or {}).get("status")
        if status == "ZERO_RESULTS":
            return None
        if status != "OK":
            raise HttpError(0, "/maps/api/geocode/json", f"status {status}")
        loc = data["results"][0]["geometry"]["location"]
        return float(loc["lat"]), float(loc["lng"])


class MapboxGeocoder:
    name = "mapbox"
    URL = "https://api.mapbox.com/geocoding/v5/mapbox.places/{q}.json"

    def __init__(self, key: str, transport: Optional[UrllibTransport] = None):
        self.key, self.t = key, transport or UrllibTransport(timeout=20, max_attempts=3)

    def geocode(self, address: str) -> Optional[LatLng]:
        url = self.URL.format(q=urllib.parse.quote(address, safe=""))
        data = self.t.request("GET", url, params=[("access_token", self.key), ("country", "us"), ("limit", 1)])
        feats = (data or {}).get("features") or []
        if not feats:
            return None
        lng, lat = feats[0]["center"]
        return float(lat), float(lng)


def make_geocoder(cfg: Config):
    if cfg.geocoder == "census":
        return CensusGeocoder()
    if cfg.geocoder == "google":
        return GoogleGeocoder(cfg.maps_api_key)
    if cfg.geocoder == "mapbox":
        return MapboxGeocoder(cfg.maps_api_key)
    return MockGeocoder()


def geocode_cached(conn, provider, address: str, now: Optional[datetime] = None) -> Tuple[Optional[float], Optional[float], str]:
    """Return (lat, lng, status) where status is 'ok', 'failed' or 'error' (transient, not cached)."""
    if not address or not address.strip():
        return None, None, "failed"
    now = now or datetime.now(timezone.utc)
    key = address_key(address)
    row = conn.execute("SELECT lat, lng, status, created_at FROM geocode_cache WHERE address_key = ?", (key,)).fetchone()
    if row:
        if row["status"] == "ok":
            return row["lat"], row["lng"], "ok"
        created = parse_iso(row["created_at"])
        if created and now - created < FAIL_RETRY:
            return None, None, "failed"
    try:
        result = provider.geocode(address)
    except Exception as e:  # transient: do not cache
        log.warning("geocode error (%s): %s", getattr(provider, "name", "?"), type(e).__name__)
        return None, None, "error"
    status = "ok" if result else "failed"
    conn.execute(
        "INSERT INTO geocode_cache(address_key, lat, lng, status, provider, created_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(address_key) DO UPDATE SET lat=excluded.lat, lng=excluded.lng, status=excluded.status, "
        "provider=excluded.provider, created_at=excluded.created_at",
        (key, result[0] if result else None, result[1] if result else None, status,
         getattr(provider, "name", "unknown"), utcnow_iso()))
    return (result[0], result[1], status) if result else (None, None, "failed")
