"""Road routing: polyline codec, OSRM / Mapbox clients, cache, fallback, failure breaker, config and /api/routes."""
import math
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.config import Config, load_config, validate_config
from app.db import Database
from app.domain.travel import HaversineTravel
from app.hcp.http import HttpError, UrllibTransport
from app.services.routing import (MapboxRouter, OsrmRouter, RoadRoutes, decode_polyline, encode_polyline,
                                  make_road_routes)
from tests.asgi_client import Client
from tests.test_api import ADMIN, DISPATCH, login, make_app

A, B, C = (33.30, -111.80), (33.40, -111.70), (33.35, -111.90)
NOW = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
TRAVEL = HaversineTravel()


def osrm_answer(a, b, seconds=600.0, meters=8046.72):
    """What an OSRM server says: an L-shaped road from a to b (a corner at (a.lat, b.lng))."""
    corner = (a[0], b[1])
    return {"code": "Ok", "waypoints": [],
            "routes": [{"duration": seconds, "distance": meters, "geometry": encode_polyline([a, corner, b])}]}


class FakeTransport:
    """Stands in for UrllibTransport: records calls and answers like OSRM / Mapbox."""

    def __init__(self, answer=osrm_answer, on_request=None):
        self.calls, self.answer, self.error, self._lock = [], answer, None, threading.Lock()
        self.on_request = on_request

    def request(self, method, url, headers=None, params=None, json_body=None, label=None):
        with self._lock:
            self.calls.append({"method": method, "url": url, "params": params, "label": label})
        if self.on_request:
            self.on_request()
        if self.error is not None:
            raise self.error
        (lng1, lat1), (lng2, lat2) = [tuple(map(float, c.split(","))) for c in url.rsplit("/", 1)[1].split(";")]
        return self.answer((lat1, lng1), (lat2, lng2))


class PolylineTests(unittest.TestCase):
    GOOGLE_EXAMPLE = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]       # from Google's polyline docs

    def test_matches_the_published_example(self):
        self.assertEqual(encode_polyline(self.GOOGLE_EXAMPLE), "_p~iF~ps|U_ulLnnqC_mqNvxq`@")
        self.assertEqual(decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@"), self.GOOGLE_EXAMPLE)

    def test_roundtrip_including_negative_and_tiny_steps(self):
        pts = [(33.30001, -111.80001), (33.30002, -111.79999), (-33.5, 151.2), (0.0, 0.0), (89.99999, -179.99999)]
        self.assertEqual(decode_polyline(encode_polyline(pts)), pts)

    def test_empty_and_truncated(self):
        self.assertEqual(decode_polyline(""), [])
        with self.assertRaises(ValueError):
            decode_polyline("_p~iF~ps|U_ulL")              # a latitude with no longitude
        with self.assertRaises(ValueError):
            decode_polyline("_p~iF~ps|U_")                 # a number cut off half way


class ProviderTests(unittest.TestCase):
    def test_osrm_request_and_answer(self):
        t = FakeTransport()
        r = OsrmRouter("http://osrm.local:5000/", t).route(A, B)
        call = t.calls[0]
        self.assertEqual(call["url"], "http://osrm.local:5000/route/v1/driving/-111.80000,33.30000;-111.70000,33.40000")
        self.assertIn(("overview", "full"), call["params"])
        self.assertIn(("geometries", "polyline"), call["params"])
        self.assertEqual((r["seconds"], r["meters"]), (600.0, 8046.72))
        self.assertEqual(decode_polyline(r["polyline"]), [A, (33.30, -111.70), B])

    def test_mapbox_request_keeps_the_key_in_the_query_and_out_of_the_label(self):
        t = FakeTransport()
        MapboxRouter("SECRET-KEY", t).route(A, B)
        call = t.calls[0]
        self.assertTrue(call["url"].startswith("https://api.mapbox.com/directions/v5/mapbox/driving/-111.80000,33.30000;"))
        self.assertIn(("access_token", "SECRET-KEY"), call["params"])
        self.assertNotIn("SECRET-KEY", call["url"] + call["label"])
        self.assertNotIn("33.3", call["label"])             # nothing customer-specific can reach a log line

    def test_no_route_is_none_and_garbage_raises(self):
        self.assertIsNone(OsrmRouter("http://x", FakeTransport(lambda a, b: {"code": "NoRoute"})).route(A, B))
        self.assertIsNone(OsrmRouter("http://x", FakeTransport(lambda a, b: {"code": "NoSegment"})).route(A, B))
        for bad in ({"code": "Ok", "routes": []}, {"code": "InvalidQuery"}, None, "oops"):
            with self.assertRaises(HttpError, msg=str(bad)):
                OsrmRouter("http://x", FakeTransport(lambda a, b, bad=bad: bad)).route(A, B)

    def test_transport_errors_carry_the_label_not_the_coordinates(self):
        t = UrllibTransport(timeout=2, max_attempts=1)
        with self.assertRaises(HttpError) as cm:
            t.request("GET", "http://127.0.0.1:9/route/v1/driving/-111.8,33.3;-111.7,33.4", label="/route/v1/driving")
        self.assertEqual(cm.exception.path, "/route/v1/driving")
        self.assertNotIn("111.8", str(cm.exception))


class RoadRoutesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "r.db"))
        self.t = FakeTransport()
        self.clock = [1000.0]
        self.rr = RoadRoutes(OsrmRouter("http://osrm.local", self.t), clock=lambda: self.clock[0])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def go(self, legs, rr=None, now=NOW):
        with self.db.session() as c:
            return (rr or self.rr).routes(c, legs, TRAVEL, now)

    def cached_rows(self):
        with self.db.session() as c:
            return c.execute("SELECT COUNT(*) FROM route_cache").fetchone()[0]

    def test_road_route_follows_the_road_and_joins_the_pins(self):
        (r,) = self.go([(A, B)])
        self.assertEqual((r["source"], r["minutes"], r["miles"]), ("road", 10.0, 5.0))
        self.assertEqual(r["path"], [[33.3, -111.8], [33.3, -111.8], [33.3, -111.7], [33.4, -111.7], [33.4, -111.7]])
        self.assertEqual(r["path"][0], list(A))
        self.assertEqual(r["path"][-1], list(B))

    def test_each_leg_is_fetched_once_even_across_restarts(self):
        self.go([(A, B)])
        self.go([(A, B)])
        self.assertEqual(len(self.t.calls), 1)
        fresh = RoadRoutes(OsrmRouter("http://osrm.local", self.t), clock=lambda: self.clock[0])   # a new process
        self.go([(A, B)], rr=fresh)
        self.assertEqual(len(self.t.calls), 1)
        self.assertEqual(self.cached_rows(), 1)

    def test_order_is_kept_and_duplicates_are_fetched_once(self):
        res = self.go([(A, B), (B, C), (A, B)])
        self.assertEqual(len(self.t.calls), 2)
        self.assertEqual(res[0], res[2])
        self.assertNotEqual(res[0], res[1])
        self.assertEqual(res[1]["path"][-1], list(C))

    def test_points_within_a_metre_share_a_cache_entry(self):
        self.go([(A, B)])
        self.go([((33.3000004, -111.8000004), B)])
        self.assertEqual(len(self.t.calls), 1)

    def test_providers_do_not_share_cache_entries(self):
        self.go([(A, B)])
        self.go([(A, B)], rr=RoadRoutes(MapboxRouter("k", self.t)))
        self.assertEqual(len(self.t.calls), 2)

    def test_same_point_costs_nothing_and_asks_nobody(self):
        (r,) = self.go([(A, A)])
        self.assertEqual((r["minutes"], r["miles"], r["source"]), (0.0, 0.0, "road"))
        self.assertEqual(self.t.calls, [])

    def test_no_provider_gives_the_same_estimate_the_slot_finder_uses(self):
        (r,) = self.go([(A, B)], rr=RoadRoutes(None))
        self.assertEqual(r["source"], "estimate")
        self.assertIsNone(r["path"])
        self.assertEqual(r["minutes"], round(TRAVEL.minutes(A, B), 1))
        self.assertEqual(r["miles"], round(TRAVEL.miles(A, B), 1))
        self.assertEqual(self.cached_rows(), 0)
        self.assertEqual(RoadRoutes(None).name, "none")

    def test_an_outage_falls_back_to_estimates_and_is_not_cached(self):
        self.t.error = HttpError(0, "/route/v1/driving", "URLError")
        (r,) = self.go([(A, B)])
        self.assertEqual((r["source"], r["path"]), ("estimate", None))
        self.assertEqual(self.cached_rows(), 0)

    def test_after_a_failure_the_provider_is_skipped_until_the_cooldown_ends(self):
        self.t.error = HttpError(503, "/route/v1/driving")
        self.go([(A, B)])
        calls = len(self.t.calls)
        self.go([(A, B), (B, C)])                                  # inside the cooldown: nobody is asked
        self.assertEqual(len(self.t.calls), calls)
        self.t.error = None
        self.clock[0] += 61                                        # provider is back and the cooldown is over
        res = self.go([(A, B)])
        self.assertEqual(res[0]["source"], "road")
        self.assertEqual(self.cached_rows(), 1)

    def test_the_database_is_not_write_locked_while_waiting_for_the_routing_server(self):
        # the background sync writes to the same SQLite file; a slow routing server must never stall it
        import sqlite3
        outcome = []

        def try_a_write_from_another_connection():
            other = sqlite3.connect(self.db.path, timeout=0.2)
            try:
                other.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('lock-probe', '1')")
                other.commit()
                outcome.append("wrote")
            except sqlite3.OperationalError as e:
                outcome.append(str(e))
            finally:
                other.close()

        rr = RoadRoutes(OsrmRouter("http://osrm.local", FakeTransport(on_request=try_a_write_from_another_connection)))
        self.go([(A, B), (B, C)], rr=rr)                           # a fresh RoadRoutes also runs its daily prune
        self.assertEqual(outcome, ["wrote", "wrote"])

    def test_one_outage_does_not_make_every_leg_wait_for_its_own_timeout(self):
        # an unreachable server: only the first leg should be tried, the other 11 are skipped at once
        rr = RoadRoutes(OsrmRouter("http://osrm.local", self.t), workers=1, clock=lambda: self.clock[0])
        self.t.error = HttpError(0, "/route/v1/driving", "timeout")
        legs = [((33.30 + i * 0.01, -111.80), (33.30 + i * 0.01, -111.70)) for i in range(12)]
        res = self.go(legs, rr=rr)
        self.assertEqual(len(self.t.calls), 1)
        self.assertTrue(all(r["source"] == "estimate" for r in res))

    def test_cached_legs_still_work_while_the_provider_is_down(self):
        self.go([(A, B)])
        self.t.error = HttpError(0, "/x")
        res = self.go([(A, B), (B, C)])
        self.assertEqual([r["source"] for r in res], ["road", "estimate"])

    def test_rejected_key_and_rate_limit_also_back_off(self):
        for status in (401, 403, 429):
            rr = RoadRoutes(OsrmRouter("http://x", FakeTransport()), clock=lambda: self.clock[0])
            rr.provider.t.error = HttpError(status, "/x")
            self.go([(A, B)], rr=rr)
            self.assertGreater(rr._down_until, self.clock[0], status)

    def test_a_pair_with_no_road_does_not_stop_the_others(self):
        def answer(a, b):
            return {"code": "NoRoute"} if b == C else osrm_answer(a, b)
        rr = RoadRoutes(OsrmRouter("http://x", FakeTransport(answer)), clock=lambda: self.clock[0])
        res = self.go([(A, C), (A, B)], rr=rr)
        self.assertEqual([r["source"] for r in res], ["estimate", "road"])
        self.assertEqual(rr._down_until, 0.0)                      # the provider is fine; that pair is just unroutable

    def test_client_errors_for_one_pair_do_not_trip_the_breaker(self):
        rr = RoadRoutes(OsrmRouter("http://x", FakeTransport()), clock=lambda: self.clock[0])
        rr.provider.t.error = HttpError(400, "/x")
        res = self.go([(A, B)], rr=rr)
        self.assertEqual(res[0]["source"], "estimate")
        self.assertEqual(rr._down_until, 0.0)

    def test_a_malformed_answer_is_not_trusted_or_cached(self):
        bad = FakeTransport(lambda a, b: {"code": "Ok", "routes": [{"duration": 5, "distance": 5, "geometry": "_"}]})
        rr = RoadRoutes(OsrmRouter("http://x", bad), clock=lambda: self.clock[0])
        self.assertEqual(self.go([(A, B)], rr=rr)[0]["source"], "estimate")
        nan = FakeTransport(lambda a, b: {"code": "Ok", "routes": [{"duration": float("nan"), "distance": 1,
                                                                    "geometry": encode_polyline([a, b])}]})
        rr = RoadRoutes(OsrmRouter("http://x", nan), clock=lambda: self.clock[0])
        self.assertEqual(self.go([(A, B)], rr=rr)[0]["source"], "estimate")
        self.assertEqual(self.cached_rows(), 0)

    def test_many_legs_are_fetched_concurrently_but_each_only_once(self):
        legs = [((33.30 + i * 0.01, -111.80), (33.30 + i * 0.01, -111.70)) for i in range(12)]
        res = self.go(legs)
        self.assertEqual(len(self.t.calls), 12)
        self.assertTrue(all(r["source"] == "road" for r in res))
        self.assertEqual(self.cached_rows(), 12)

    def test_cache_entries_expire_and_old_rows_are_pruned(self):
        self.go([(A, B)])
        later = NOW + timedelta(days=31)
        self.go([(A, B)], now=later)
        self.assertEqual(len(self.t.calls), 2)                     # stale entry was refetched
        self.assertEqual(self.cached_rows(), 1)

    def test_factory_follows_the_config(self):
        self.assertEqual(make_road_routes(Config(router="none")).name, "none")
        osrm = make_road_routes(Config(router="osrm", router_url="http://osrm.local:5000"))
        self.assertEqual((osrm.name, osrm.provider.base), ("osrm", "http://osrm.local:5000"))
        self.assertEqual(make_road_routes(Config(router="mapbox", maps_api_key="")).name, "none")   # no key: stay off
        self.assertEqual(make_road_routes(Config(router="mapbox", maps_api_key="k")).name, "mapbox")


class RouterConfigTests(unittest.TestCase):
    def load(self, **env):
        with mock.patch.dict(os.environ, env, clear=True):
            return load_config()

    def test_demo_data_defaults_to_road_routing_but_live_data_is_opt_in(self):
        self.assertEqual(Config().router, "none")                                   # tests / bare Config never go online
        self.assertEqual(self.load().router, "osrm")                                # no settings at all = demo mode
        self.assertEqual(self.load(HCP_MODE="mock").router, "osrm")
        self.assertEqual(self.load(HCP_MODE="live", HCP_API_KEY="k", GEOCODER="census").router, "none")
        self.assertEqual(self.load(HCP_MODE="live", HCP_API_KEY="k", GEOCODER="census", ROUTER="mapbox",
                                   MAPS_API_KEY="m").router, "mapbox")
        self.assertEqual(self.load(ROUTER="NONE").router, "none")                   # case-insensitive, and can be turned off
        self.assertEqual(self.load(ROUTER_URL="http://osrm.local:5000/").router_url, "http://osrm.local:5000")

    def test_validation(self):
        ok = Config()
        self.assertFalse([p for p in validate_config(ok) if "ROUTER" in p])
        self.assertTrue([p for p in validate_config(Config(router="bing")) if "ROUTER must be" in p])
        self.assertTrue([p for p in validate_config(Config(router="mapbox")) if "needs MAPS_API_KEY" in p and "ROUTER" in p])
        self.assertTrue([p for p in validate_config(Config(router="osrm", router_url="ftp://x")) if "ROUTER_URL" in p])
        live = Config(hcp_mode="live", hcp_api_key="k", geocoder="census", router="osrm")
        self.assertTrue([p for p in validate_config(live) if "public OSRM demo server" in p])
        own = Config(hcp_mode="live", hcp_api_key="k", geocoder="census", router="osrm", router_url="http://osrm.local")
        self.assertFalse([p for p in validate_config(own) if "public OSRM" in p])

    def test_the_map_key_never_shows_in_the_config_repr(self):
        self.assertNotIn("SECRET", repr(Config(router="mapbox", maps_api_key="SECRET")))


class RoutesApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = FakeTransport()
        cls.app, cls.tmp = make_app(routes=RoadRoutes(OsrmRouter("http://osrm.local", cls.t)))
        cls.admin = login(cls.app, ADMIN)
        cls.admin.post("/api/users", {"email": DISPATCH[0], "password": DISPATCH[1], "role": "dispatcher", "name": "Dee"})
        cls.disp = login(cls.app, DISPATCH)
        cls.off_app, cls.off_tmp = make_app()                     # default Config: road routing off
        cls.off = login(cls.off_app, ADMIN)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        shutil.rmtree(cls.off_tmp, ignore_errors=True)

    def leg(self, a=A, b=B):
        return {"a": list(a), "b": list(b)}

    def test_login_and_csrf_header_are_required(self):
        self.assertEqual(Client(self.app).post("/api/routes", {"legs": [self.leg()]}).status, 401)
        c = login(self.app, DISPATCH)
        self.assertEqual(c.post("/api/routes", {"legs": [self.leg()]}, csrf=False).status, 403)
        self.assertEqual(c.get("/api/routes").status, 405)

    def test_road_routes_with_a_path_and_drive_time(self):
        r = self.disp.post("/api/routes", {"legs": [self.leg(A, B), self.leg(B, C)]})
        self.assertEqual(r.status, 200, r.text)
        res = r.json()
        self.assertEqual(res["provider"], "osrm")
        self.assertEqual([x["source"] for x in res["routes"]], ["road", "road"])
        self.assertEqual(res["routes"][0]["minutes"], 10.0)
        self.assertGreater(len(res["routes"][0]["path"]), 2)
        self.assertEqual(res["routes"][1]["path"][-1], list(C))

    def test_repeat_requests_come_from_the_cache(self):
        leg = self.leg((33.51, -111.61), (33.52, -111.62))
        self.disp.post("/api/routes", {"legs": [leg]})
        before = len(self.t.calls)
        self.disp.post("/api/routes", {"legs": [leg]})
        self.assertEqual(len(self.t.calls), before)

    def test_when_the_provider_is_down_the_answer_is_still_200_with_estimates(self):
        app, tmp = make_app(routes=RoadRoutes(OsrmRouter("http://osrm.local", FakeTransport())))
        try:
            app.state.routes.provider.t.error = HttpError(0, "/route/v1/driving", "URLError")
            c = login(app, ADMIN)
            with self.assertLogs("routing.routes", level="WARNING") as logs:
                r = c.post("/api/routes", {"legs": [self.leg()]})
            self.assertEqual(r.status, 200)
            self.assertEqual((r.json()["routes"][0]["source"], r.json()["routes"][0]["path"]), ("estimate", None))
            self.assertIn("unavailable", "\n".join(logs.output))
            self.assertNotIn("111.7", "\n".join(logs.output))     # no customer locations in the log
            self.assertNotIn("33.4", "\n".join(logs.output))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_with_road_routing_off_you_get_the_slot_finder_estimate(self):
        r = self.off.post("/api/routes", {"legs": [self.leg()]}).json()
        self.assertEqual(r["provider"], "none")
        x = r["routes"][0]
        self.assertEqual((x["source"], x["path"]), ("estimate", None))
        self.assertEqual(x["minutes"], round(HaversineTravel().minutes(A, B), 1))

    def test_config_tells_the_page_which_provider_is_on(self):
        self.assertEqual(self.disp.get("/api/config").json()["routing"], {"provider": "osrm"})
        self.assertEqual(self.off.get("/api/config").json()["routing"], {"provider": "none"})

    def test_validation(self):
        good = self.leg()
        bad_bodies = [{}, {"legs": []}, {"legs": "x"}, {"legs": [good] * 81}, {"legs": [5]}, {"legs": [{"a": [1, 2]}]},
                      {"legs": [{"a": [1, 2], "b": "x"}]}, {"legs": [{"a": [1], "b": [1, 2]}]},
                      {"legs": [{"a": [1, 2, 3], "b": [1, 2]}]}, {"legs": [{"a": ["1", "2"], "b": [1, 2]}]},
                      {"legs": [{"a": [True, False], "b": [1, 2]}]}, {"legs": [{"a": [91, 0], "b": [1, 2]}]},
                      {"legs": [{"a": [0, 181], "b": [1, 2]}]}, {"legs": [{"a": [0, -181], "b": [1, 2]}]},
                      {"legs": [{"a": [None, 0], "b": [1, 2]}]}]
        for body in bad_bodies:
            self.assertEqual(self.disp.post("/api/routes", body).status, 400, str(body)[:80])
        nan = self.disp.request("POST", "/api/routes", {"legs": [{"a": [math.nan, 0], "b": [1, 2]}]})   # JSON "NaN"
        self.assertEqual(nan.status, 400)
        self.assertEqual(self.disp.post("/api/routes", {"legs": [good] * 80}).status, 200)       # the limit itself is fine

    def test_the_provider_key_is_never_in_a_response(self):
        app, tmp = make_app(routes=RoadRoutes(MapboxRouter("MAPBOX-SECRET-KEY", FakeTransport())))
        try:
            r = login(app, ADMIN).post("/api/routes", {"legs": [self.leg()]})
            self.assertEqual(r.status, 200)
            self.assertNotIn("MAPBOX-SECRET-KEY", r.text)
            self.assertNotIn("MAPBOX-SECRET-KEY", login(app, ADMIN).get("/api/config").text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
