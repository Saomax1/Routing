"""
Travel-time estimation behind a small interface.

The slot finder only calls ``provider.minutes(a, b)``. The default ``HaversineTravel`` estimates
drive time from straight-line distance (fast, free, no API key, good enough to rank options).
To use real road times later, implement the same interface on top of the Google Distance Matrix /
Mapbox Matrix API, or a self-hosted VROOM / OpenRouteService / OSRM, and pass it to the
slot finder - nothing else changes.
"""

from __future__ import annotations

import math
from typing import Protocol, Tuple

LatLng = Tuple[float, float]


class TravelTimeProvider(Protocol):
    def minutes(self, a: LatLng, b: LatLng) -> float: ...


def haversine_miles(a: LatLng, b: LatLng) -> float:
    r = 3958.8
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = la2 - la1, lo2 - lo1
    h = math.sin(dlat / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


class HaversineTravel:
    """Straight-line distance x circuity factor / average speed. Same point => 0 minutes."""

    def __init__(self, speed_mph: float = 28.0, circuity: float = 1.3, min_minutes: float = 3.0):
        self.speed_mph = max(1.0, float(speed_mph))
        self.circuity = max(1.0, float(circuity))
        self.min_minutes = float(min_minutes)

    def miles(self, a: LatLng, b: LatLng) -> float:
        return haversine_miles(a, b) * self.circuity

    def minutes(self, a: LatLng, b: LatLng) -> float:
        d = self.miles(a, b)
        if d < 0.1:
            return 0.0
        return max(self.min_minutes, d / self.speed_mph * 60.0)

    @classmethod
    def from_settings(cls, settings: dict) -> "HaversineTravel":
        s = settings.get("scheduling", {})
        return cls(s.get("travel_speed_mph", 28), s.get("travel_circuity", 1.3), s.get("min_travel_minutes", 3))
