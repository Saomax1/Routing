"""
Road routes between two points: the map lines that follow roads and the drive time shown when you hover them.

Display only. The slot finder still ranks with the straight-line estimate in ``domain/travel.py``.

Providers (ROUTER env var):
  none    no road routing: lines stay straight and times are the straight-line estimate
  osrm    any OSRM server (ROUTER_URL; default is the public demo server, which is fair-use only)
  mapbox  Mapbox Directions API (MAPS_API_KEY)

Every answer is cached in SQLite (roads rarely change), so a leg is fetched once. If the provider is off,
rejecting us or unreachable, a leg falls back to the same straight-line estimate the slot finder uses and the
response says so (``source: "estimate"``). After a transient failure the provider is skipped for a minute so a
dead server cannot make every request wait for a timeout.

SECURITY: coordinates are customer locations. They are sent only to the configured provider, and never logged:
the HTTP transport is given a fixed ``label`` so error messages carry no coordinates, and the API key stays in
the query string, which the transport never logs.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..domain.timeutil import parse_iso
from ..hcp.http import HttpError, UrllibTransport

log = logging.getLogger("routing.routes")

LatLng = Tuple[float, float]
METERS_PER_MILE = 1609.344
CACHE_TTL = timedelta(days=30)
COOLDOWN_SECONDS = 60.0
MAX_WORKERS = 4
# HTTP statuses that mean "the provider is unusable right now" (as opposed to "no route for this pair")
_PROVIDER_DOWN = {0, 401, 403, 429}


# --------------------------------------------------------------------------- polyline (Google format, 5 decimals)

def encode_polyline(points: Sequence[LatLng], precision: int = 5) -> str:
    factor = 10 ** precision
    out: List[str] = []
    plat = plng = 0
    for lat, lng in points:
        ilat, ilng = round(lat * factor), round(lng * factor)
        for v in (ilat - plat, ilng - plng):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plng = ilat, ilng
    return "".join(out)


def decode_polyline(encoded: str, precision: int = 5) -> List[LatLng]:
    factor = 10 ** precision
    pts: List[LatLng] = []
    i = lat = lng = 0
    while i < len(encoded):
        for axis in (0, 1):
            shift = result = 0
            while True:
                if i >= len(encoded):
                    raise ValueError("truncated polyline")
                b = ord(encoded[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += delta
            else:
                lng += delta
        pts.append((lat / factor, lng / factor))
    return pts


# ------------------------------------------------------------------------------------------------ providers

class _DirectionsRouter:
    """OSRM and Mapbox take the same kind of request and answer ``routes[0].{duration, distance, geometry}``."""

    name = ""
    label = ""

    def __init__(self, transport: Optional[UrllibTransport] = None):
        self.t = transport or UrllibTransport(timeout=6, max_attempts=2, base_delay=0.5)

    def _url(self, coords: str) -> str:
        raise NotImplementedError

    def _params(self) -> list:
        raise NotImplementedError

    def route(self, a: LatLng, b: LatLng) -> Optional[dict]:
        """{"seconds", "meters", "polyline"} or None when there is no road route between the points."""
        coords = f"{a[1]:.5f},{a[0]:.5f};{b[1]:.5f},{b[0]:.5f}"          # lng,lat;lng,lat
        data = self.t.request("GET", self._url(coords), params=self._params(), label=self.label)
        code = data.get("code") if isinstance(data, dict) else None
        if code in ("NoRoute", "NoSegment"):
            return None
        routes = data.get("routes") if isinstance(data, dict) else None
        if code != "Ok" or not routes:
            raise HttpError(0, self.label, f"unexpected answer ({code})")
        r = routes[0]
        return {"seconds": float(r["duration"]), "meters": float(r["distance"]), "polyline": str(r["geometry"])}


class OsrmRouter(_DirectionsRouter):
    name = "osrm"
    label = "/route/v1/driving"

    def __init__(self, base_url: str, transport: Optional[UrllibTransport] = None):
        super().__init__(transport)
        self.base = base_url.rstrip("/")

    def _url(self, coords: str) -> str:
        return f"{self.base}/route/v1/driving/{coords}"

    def _params(self) -> list:
        return [("overview", "full"), ("geometries", "polyline"), ("steps", "false"), ("alternatives", "false")]


class MapboxRouter(_DirectionsRouter):
    name = "mapbox"
    label = "/directions/v5/mapbox/driving"

    def __init__(self, key: str, transport: Optional[UrllibTransport] = None):
        super().__init__(transport)
        self.key = key

    def _url(self, coords: str) -> str:
        return f"https://api.mapbox.com/directions/v5/mapbox/driving/{coords}"

    def _params(self) -> list:
        return [("overview", "full"), ("geometries", "polyline"), ("access_token", self.key)]


def make_road_routes(cfg) -> "RoadRoutes":
    if cfg.router == "osrm":
        return RoadRoutes(OsrmRouter(cfg.router_url))
    if cfg.router == "mapbox" and cfg.maps_api_key:
        return RoadRoutes(MapboxRouter(cfg.maps_api_key))
    return RoadRoutes(None)


# -------------------------------------------------------------------------------------------- cache + fallback

class RoadRoutes:
    """Looks legs up in the cache, fetches the missing ones concurrently, and falls back to estimates."""

    def __init__(self, provider=None, workers: int = MAX_WORKERS, cooldown: float = COOLDOWN_SECONDS,
                 ttl: timedelta = CACHE_TTL, clock: Callable[[], float] = time.monotonic):
        self.provider, self.workers, self.cooldown, self.ttl, self.clock = provider, workers, cooldown, ttl, clock
        self._down_until = 0.0
        self._lock = threading.Lock()
        self._last_prune: Optional[date] = None

    @property
    def name(self) -> str:
        return getattr(self.provider, "name", "none")

    @staticmethod
    def _round(p: Sequence[float]) -> LatLng:
        return round(float(p[0]), 5), round(float(p[1]), 5)

    def _key(self, a: LatLng, b: LatLng) -> str:
        return f"{self.name}|{a[0]:.5f},{a[1]:.5f}|{b[0]:.5f},{b[1]:.5f}"

    def routes(self, conn, legs: Sequence[Tuple[Sequence[float], Sequence[float]]], travel,
               now: Optional[datetime] = None) -> List[dict]:
        """One result per leg, same order: {"minutes", "miles", "path": [[lat, lng], ...] | None, "source"}."""
        now = now or datetime.now(timezone.utc)
        keyed = [(self._round(a), self._round(b)) for a, b in legs]
        found: Dict[str, dict] = {}
        if self.provider is not None:
            wanted = {self._key(a, b): (a, b) for a, b in keyed if a != b}
            found = self._from_cache(conn, list(wanted), now)
            missing = {k: ab for k, ab in wanted.items() if k not in found}
            if missing:
                found.update(self._fetch(conn, missing, now))
            # writes go last: nothing may hold SQLite's write lock while we wait on the routing server (it would
            # stall the background sync), so the first write of the request happens only after all network I/O
            self._maybe_prune(conn, now)
        return [self._compose(a, b, found.get(self._key(a, b)), travel) for a, b in keyed]

    # -- cache
    def _from_cache(self, conn, keys: List[str], now: datetime) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for i in range(0, len(keys), 200):
            chunk = keys[i:i + 200]
            rows = conn.execute("SELECT key, seconds, meters, polyline, created_at FROM route_cache WHERE key IN (%s)"
                                % ",".join("?" * len(chunk)), chunk).fetchall()
            for r in rows:
                created = parse_iso(r["created_at"])
                if created and now - created < self.ttl:
                    out[r["key"]] = {"seconds": r["seconds"], "meters": r["meters"], "polyline": r["polyline"]}
        return out

    def _maybe_prune(self, conn, now: datetime) -> None:
        if self._last_prune != now.date():
            cutoff = (now - self.ttl).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute("DELETE FROM route_cache WHERE created_at < ?", (cutoff,))
            self._last_prune = now.date()

    # -- provider
    def _trip(self, err: Exception) -> None:
        with self._lock:
            first = self.clock() >= self._down_until
            self._down_until = self.clock() + self.cooldown
        if first:
            status = getattr(err, "status", None)
            log.warning("road routing unavailable (%s%s): using straight-line estimates for %d s",
                        type(err).__name__, f" {status}" if status else "", self.cooldown)

    def _fetch_one(self, a: LatLng, b: LatLng) -> Optional[dict]:
        if self.clock() < self._down_until:
            return None
        try:
            r = self.provider.route(a, b)
            if r is None:
                return None                                   # genuinely no road route for this pair
            if not (math.isfinite(r["seconds"]) and math.isfinite(r["meters"]) and r["seconds"] >= 0
                    and len(decode_polyline(r["polyline"])) >= 2):
                raise ValueError("bad route")
            return r
        except HttpError as e:
            if e.status in _PROVIDER_DOWN or e.status >= 500:
                self._trip(e)
            return None                                       # other 4xx: this pair is unroutable, provider is fine
        except Exception as e:                                # malformed answer, parse error, ...
            self._trip(e)
            return None

    def _fetch(self, conn, missing: Dict[str, Tuple[LatLng, LatLng]], now: datetime) -> Dict[str, dict]:
        if self.clock() < self._down_until:
            return {}
        items = list(missing.items())
        with ThreadPoolExecutor(max_workers=max(1, min(self.workers, len(items)))) as pool:
            answers = list(pool.map(lambda kv: self._fetch_one(*kv[1]), items))
        out: Dict[str, dict] = {}
        for (key, _), r in zip(items, answers):
            if r is None:
                continue
            out[key] = r
            conn.execute("INSERT INTO route_cache(key, seconds, meters, polyline, created_at) VALUES (?,?,?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET seconds = excluded.seconds, meters = excluded.meters, "
                         "polyline = excluded.polyline, created_at = excluded.created_at",
                         (key, r["seconds"], r["meters"], r["polyline"], now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
        return out

    # -- result
    def _compose(self, a: LatLng, b: LatLng, row: Optional[dict], travel) -> dict:
        if row:
            # the road snaps to the nearest street: join the line to the actual pins at both ends
            path = [a, *decode_polyline(row["polyline"]), b]
            return {"minutes": round(row["seconds"] / 60.0, 1), "miles": round(row["meters"] / METERS_PER_MILE, 1),
                    "path": [[round(la, 5), round(ln, 5)] for la, ln in path], "source": "road"}
        if a == b and self.provider is not None:
            return {"minutes": 0.0, "miles": 0.0, "path": None, "source": "road"}
        return {"minutes": round(travel.minutes(a, b), 1), "miles": round(travel.miles(a, b), 1),
                "path": None, "source": "estimate"}
