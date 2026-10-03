"""Going live: read-only access to Housecall Pro over real HTTP, real data replacing the demo, and the live check."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import Config
from app.db import Database
from app.domain.travel import HaversineTravel
from app.hcp.client import HCPClient, MockHCPClient
from app.hcp.fixtures import DEMO_TECH_SETUP
from app.hcp.http import HttpError, ReadOnlyTransport, ReadOnlyViolation, UrllibTransport
from app.main import create_app
from app.services.data_mode import count_demo, count_real, has_real_data, purge_demo_data
from app.services.geocode import MockGeocoder
from app.services.live_check import check_option, run_live_check
from app.services.live_setup import check_key, update_env_file
from app.services.routing import RoadRoutes, encode_polyline
from app.services.settings_store import DEFAULT_SETTINGS
from app.services.sync import SyncService
from tests.fake_hcp import FakeHcp

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)          # Thu 08:00 America/Phoenix
KEY = "test-key-123"
TZ = ZoneInfo("America/Phoenix")


class LiveEnv:
    """A stand-in Housecall Pro, a live-mode config pointing at it, and an empty database."""

    def __init__(self, key=KEY, server_key=KEY, page_size=5):
        self.tmp = tempfile.mkdtemp()
        self.fake = FakeHcp(key=server_key, now=NOW).start()
        self.cfg = Config(hcp_mode="live", hcp_api_key=key, hcp_base_url=self.fake.url, hcp_page_size=page_size,
                          database_path=os.path.join(self.tmp, "live.db"), session_secret="s", geocoder="mock")
        self.db = Database(self.cfg.database_path)

    def client(self):
        return HCPClient(self.cfg)

    def live_sync(self):
        return SyncService(self.db, self.cfg, self.client(), MockGeocoder(), new_tech_defaults=None).run(NOW)

    def seed_demo(self):
        """What an earlier demo run leaves behind: fake jobs, technicians, caches, a booking and a note."""
        demo_cfg = Config(database_path=self.cfg.database_path)               # mock mode
        SyncService(self.db, demo_cfg, MockHCPClient(now=NOW), MockGeocoder(),
                    new_tech_defaults=lambda e: DEMO_TECH_SETUP.get(e)).run(NOW)
        with self.db.session() as c:
            c.execute("INSERT INTO users(email, name, password_hash, role, created_at) VALUES "
                      "('admin@example.com', 'Admin', 'x', 'admin', '2026-01-01T00:00:00Z')")
            c.execute("INSERT INTO job_exceptions(hcp_job_id, reason, note, set_at) VALUES ('job_demo_033', 'other', 'x', '2026-10-01T00:00:00Z')")
            c.execute("INSERT INTO bookings(hcp_job_id, tech_id, arrive_at, end_at, window_start_at, window_end_at, duration_min, booked_at) "
                      "VALUES ('job_demo_033', 'emp_demo_1', '2026-10-02T17:00:00Z', '2026-10-02T18:30:00Z', '2026-10-02T16:00:00Z', "
                      "'2026-10-02T20:00:00Z', 90, '2026-10-01T15:00:00Z')")
            c.execute("INSERT INTO schedule_actions(at, hcp_job_id, new_tech, success) VALUES ('2026-10-01T15:00:00Z', 'job_demo_033', 'emp_demo_1', 1)")
            c.execute("INSERT INTO route_cache(key, seconds, meters, polyline, created_at) VALUES ('k', 1, 1, 'x', '2026-10-01T00:00:00Z')")

    def q(self, sql, *args):
        with self.db.session() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def close(self):
        self.fake.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class ReadOnlyOverTheWireTests(unittest.TestCase):
    def setUp(self):
        self.env = LiveEnv()

    def tearDown(self):
        self.env.close()

    def test_every_read_works_over_http_and_nothing_but_gets_is_sent(self):
        c, ds = self.env.client(), self.env.fake.dataset
        employees, unscheduled = c.list_employees(), c.list_unscheduled()
        scheduled = c.list_scheduled(date(2026, 10, 1), date(2026, 10, 15), TZ)
        done = c.list_completed(date(2026, 9, 28), date(2026, 10, 1), TZ)
        self.assertEqual(len(employees), len(ds["employees"]))
        self.assertEqual(len(unscheduled), sum(j["work_status"] == "unscheduled" for j in ds["jobs"]))
        self.assertTrue(scheduled and {j["work_status"] for j in scheduled} <= {"scheduled", "in progress"})
        self.assertTrue(done and all(j["work_status"].startswith("complete") for j in done))
        reqs = self.env.fake.requests
        self.assertGreater(len([r for r in reqs if r["path"] == "/jobs" and r["query"]["page"] == ["2"]]), 0)   # paged (5 a page)
        self.assertEqual(self.env.fake.methods(), {"GET"})
        self.assertTrue(all(r["authorization"] == f"Token {KEY}" for r in reqs))
        for r in reqs:                                                      # the key travels in the header only
            self.assertNotIn(KEY, r["path"] + json.dumps(r["query"]))

    def test_a_full_live_sync_only_reads(self):
        self.assertEqual(self.env.live_sync()["status"], "ok")
        paths = {(r["method"], r["path"]) for r in self.env.fake.requests}
        self.assertEqual(paths, {("GET", "/employees"), ("GET", "/jobs")})

    def test_writes_are_refused_before_anything_is_sent(self):
        c = self.env.client()
        for method in ("POST", "PUT", "PATCH", "DELETE", "post"):
            with self.subTest(method=method):
                with self.assertRaises(ReadOnlyViolation):
                    c.transport.request(method, f"{self.env.fake.url}/jobs/job_x/schedule", headers={}, json_body={"a": 1})
        with self.assertRaises(ReadOnlyViolation):                          # a GET carrying a body is not a read either
            c.transport.request("GET", f"{self.env.fake.url}/jobs", json_body={"a": 1})
        self.assertEqual(self.env.fake.requests, [])                        # nothing reached the server
        with self.assertRaises(NotImplementedError):
            c.set_schedule("job", None)
        with self.assertRaises(NotImplementedError):
            c.assign_employees("job", [])

    def test_even_a_raw_transport_handed_to_the_client_is_wrapped(self):
        c = HCPClient(self.env.cfg, transport=UrllibTransport())
        self.assertIsInstance(c.transport, ReadOnlyTransport)
        with self.assertRaises(ReadOnlyViolation):
            c.transport.request("DELETE", f"{self.env.fake.url}/jobs/x")
        self.assertEqual(self.env.fake.requests, [])

    def test_the_client_has_no_way_to_write(self):
        """If someone adds a method that is not a plain read, this fails and makes them think about it."""
        public = {n for n in dir(HCPClient) if not n.startswith("_") and callable(getattr(HCPClient, n))}
        self.assertEqual(public, {"list_employees", "list_unscheduled", "list_scheduled", "list_completed",
                                  "set_schedule", "assign_employees"})


class KeyCheckTests(unittest.TestCase):
    def test_a_good_key(self):
        env = LiveEnv()
        try:
            r = check_key(env.cfg)
            self.assertEqual((r["ok"], r["employees"] > 0), (True, True))
        finally:
            env.close()

    def test_a_rejected_key_is_explained_and_never_echoed(self):
        env = LiveEnv(key="WRONG-key-999")
        try:
            r = check_key(env.cfg)
            self.assertFalse(r["ok"])
            self.assertIn("401", r["message"])
            self.assertNotIn("WRONG-key-999", r["message"])
        finally:
            env.close()

    def test_no_key_and_no_network(self):
        self.assertFalse(check_key(Config(hcp_mode="live", hcp_api_key=""))["ok"])
        r = check_key(Config(hcp_mode="live", hcp_api_key="k", hcp_base_url="http://127.0.0.1:9"),
                      transport=type("T", (), {"request": lambda *a, **k: (_ for _ in ()).throw(HttpError(0, "/employees", "URLError"))})())
        self.assertFalse(r["ok"])
        self.assertIn("Network", r["message"])


class DemoDataTests(unittest.TestCase):
    def setUp(self):
        self.env = LiveEnv()
        self.env.seed_demo()

    def tearDown(self):
        self.env.close()

    def test_real_jobs_replace_the_demo_data(self):
        with self.env.db.session() as c:
            self.assertTrue(count_demo(c)["jobs"] > 20 and not has_real_data(c))
        res = self.env.live_sync()
        self.assertEqual(res["status"], "ok")
        ds = self.env.fake.dataset
        with self.env.db.session() as c:
            self.assertEqual(count_demo(c), {"jobs": 0, "technicians": 0})
            self.assertEqual(count_real(c), {"jobs": len(ds["jobs"]), "technicians": len(ds["employees"])})
        self.assertEqual(self.env.q("SELECT 1 FROM bookings"), [])          # the fake booking and note went with their job
        self.assertEqual(self.env.q("SELECT 1 FROM job_exceptions"), [])
        self.assertEqual(self.env.q("SELECT 1 FROM schedule_actions"), [])
        self.assertEqual(self.env.q("SELECT 1 FROM route_cache"), [])
        self.assertEqual(self.env.q("SELECT 1 FROM sync_runs WHERE mode = 'mock'"), [])
        self.assertEqual([u["email"] for u in self.env.q("SELECT email FROM users")], ["admin@example.com"])   # logins are kept

    def test_real_technicians_start_with_routing_off(self):
        self.env.live_sync()
        techs = self.env.q("SELECT active, trade_skills, home_lat FROM technicians")
        self.assertTrue(techs)
        self.assertEqual({(t["active"], t["trade_skills"], t["home_lat"]) for t in techs}, {(0, "[]", None)})

    def test_a_wrong_key_still_clears_the_demo_data_and_shows_no_secret(self):
        env = LiveEnv(key="WRONG-key-999")
        try:
            env.seed_demo()
            res = env.live_sync()
            self.assertEqual(res["status"], "error")
            self.assertNotIn("WRONG-key-999", json.dumps(res))
            with env.db.session() as c:
                self.assertEqual(count_demo(c), {"jobs": 0, "technicians": 0})      # fake customers are never shown in live mode
        finally:
            env.close()

    def test_demo_mode_refuses_a_database_holding_real_data(self):
        self.env.live_sync()
        before = self.env.q("SELECT COUNT(*) AS n FROM jobs")[0]["n"]
        demo = SyncService(self.env.db, Config(database_path=self.env.cfg.database_path), MockHCPClient(now=NOW), MockGeocoder())
        res = demo.run(NOW)
        self.assertEqual(res["status"], "error")
        self.assertIn("real Housecall Pro data", res["error"])
        self.assertEqual(self.env.q("SELECT COUNT(*) AS n FROM jobs")[0]["n"], before)
        with self.env.db.session() as c:
            self.assertEqual(count_demo(c)["jobs"], 0)

    def test_starting_the_app_in_live_mode_clears_the_demo_data(self):
        create_app(self.env.cfg, hcp=self.env.client(), geocoder=MockGeocoder(), background_sync=False)
        with self.env.db.session() as c:
            self.assertEqual(count_demo(c), {"jobs": 0, "technicians": 0})

    def test_starting_the_app_in_demo_mode_leaves_demo_data_alone(self):
        create_app(Config(database_path=self.env.cfg.database_path, session_secret="s"), background_sync=False)
        with self.env.db.session() as c:
            self.assertGreater(count_demo(c)["jobs"], 20)


class PurgeRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "p.db"))
        with self.db.session() as c:
            for jid in ("job_demo_001", "job_9f8e7d", "jobXdemoXlookalike"):
                c.execute("INSERT INTO jobs(hcp_job_id, work_status) VALUES (?, 'unscheduled')", (jid,))
                c.execute("INSERT INTO warranty_details(hcp_job_id) VALUES (?)", (jid,))
                c.execute("INSERT INTO job_exceptions(hcp_job_id, reason, set_at) VALUES (?, 'other', 'x')", (jid,))
                c.execute("INSERT INTO schedule_actions(at, hcp_job_id, success) VALUES ('x', ?, 1)", (jid,))
            for tid in ("emp_demo_1", "pro_77aa"):
                c.execute("INSERT INTO technicians(hcp_employee_id, name) VALUES (?, 'T')", (tid,))
            c.execute("INSERT INTO geocode_cache(address_key, status, provider, created_at) VALUES ('a', 'ok', 'm', 'x')")
            c.execute("INSERT INTO route_cache(key, seconds, meters, polyline, created_at) VALUES ('k', 1, 1, 'x', 'x')")
            c.execute("INSERT INTO sync_runs(started_at, mode) VALUES ('x', 'mock')")
            c.execute("INSERT INTO sync_runs(started_at, mode) VALUES ('x', 'live')")
            c.execute("INSERT INTO users(email, name, password_hash, role, created_at) VALUES ('me@x.co', 'Me', 'h', 'admin', 'x')")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def n(self, table, where="1"):
        with self.db.session() as c:
            return c.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]

    def test_only_demo_rows_go(self):
        with self.db.session() as c:
            self.assertEqual(purge_demo_data(c), {"jobs": 1, "technicians": 1})
        self.assertEqual(self.ids("jobs", "hcp_job_id"), sorted(["job_9f8e7d", "jobXdemoXlookalike"]))        # "_" is not a wildcard
        for table in ("warranty_details", "job_exceptions", "schedule_actions"):
            self.assertEqual(self.n(table), 2, table)
            self.assertEqual(self.n(table, "hcp_job_id = 'job_demo_001'"), 0, table)
        self.assertEqual(self.ids("technicians", "hcp_employee_id"), ["pro_77aa"])
        self.assertEqual((self.n("geocode_cache"), self.n("route_cache")), (0, 0))
        self.assertEqual(self.ids("sync_runs", "mode"), ["live"])
        self.assertEqual(self.n("users"), 1)

    def test_nothing_to_remove_touches_nothing(self):
        with self.db.session() as c:
            purge_demo_data(c)
            c.execute("INSERT INTO geocode_cache(address_key, status, provider, created_at) VALUES ('real', 'ok', 'census', 'x')")
            self.assertEqual(purge_demo_data(c), {"jobs": 0, "technicians": 0})
        self.assertEqual(self.n("geocode_cache"), 1)                                   # a real cache survives a no-op

    def ids(self, table, col):
        with self.db.session() as c:
            return sorted(r[0] for r in c.execute(f"SELECT {col} FROM {table}"))


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = self.tmp / ".env"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_existing_and_commented_settings_are_switched_and_the_rest_is_kept(self):
        self.env.write_text("# my notes\nHCP_MODE=mock\nHCP_API_KEY=\n# ROUTER=osrm\nADMIN_EMAIL=me@x.co\n", encoding="utf-8")
        update_env_file(self.env, {"HCP_MODE": "live", "HCP_API_KEY": "abc123", "ROUTER": "none", "GEOCODER": "census"})
        self.assertEqual(self.env.read_text(encoding="utf-8").splitlines(),
                         ["# my notes", "HCP_MODE=live", "HCP_API_KEY=abc123", "ROUTER=none", "ADMIN_EMAIL=me@x.co", "GEOCODER=census"])

    def test_a_missing_file_starts_from_the_template(self):
        template = self.tmp / ".env.example"
        template.write_text("# Copy to .env\nHCP_MODE=mock\nHCP_API_KEY=\n", encoding="utf-8")
        update_env_file(self.env, {"HCP_MODE": "live", "HCP_API_KEY": "k"}, template=template)
        self.assertEqual(self.env.read_text(encoding="utf-8").splitlines(), ["# Copy to .env", "HCP_MODE=live", "HCP_API_KEY=k"])
        self.assertEqual(template.read_text(encoding="utf-8").count("live"), 0)       # the template is not edited

    def test_values_that_need_quotes_round_trip_through_dotenv(self):
        from dotenv import dotenv_values
        update_env_file(self.env, {"A": "plain-value_1.2", "B": 'has space # and "quote"', "C": ""})
        got = dotenv_values(self.env)
        self.assertEqual((got["A"], got["B"], got["C"]), ("plain-value_1.2", 'has space # and "quote"', ""))

    def test_no_temporary_file_is_left_and_the_file_is_private(self):
        update_env_file(self.env, {"HCP_API_KEY": "secret"})
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [".env"])
        if os.name == "posix":
            self.assertEqual(self.env.stat().st_mode & 0o077, 0)


class Script:
    """Scripted answers for scripts/go_live.py."""

    def __init__(self, asks=(), secrets=()):
        self.asks, self.secrets, self.out = list(asks), list(secrets), []

    def say(self, text=""):
        self.out.append(str(text))

    def ask(self, prompt, default=""):
        self.out.append(f"? {prompt}")
        return (self.asks.pop(0) if self.asks else default) or default

    def secret(self, prompt):
        self.out.append(f"? {prompt} (hidden)")
        return self.secrets.pop(0) if self.secrets else ""

    @property
    def text(self):
        return "\n".join(self.out)


def load_go_live():
    spec = importlib.util.spec_from_file_location("go_live_under_test", ROOT / "scripts" / "go_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class GoLiveScriptTests(unittest.TestCase):
    def setUp(self):
        self.env = LiveEnv()
        self.env.seed_demo()
        self.env_file = Path(self.env.tmp) / ".env"
        self.mod = load_go_live()

    def tearDown(self):
        self.env.close()

    def go(self, io, **kw):
        return self.mod.run(io, env_path=self.env_file, database_path=self.env.cfg.database_path,
                            base_url=self.env.fake.url, **kw)

    def test_the_happy_path_with_defaults(self):
        # key -> geocoder 1 (census) -> routes 1 (none) -> create my own login
        io = Script(asks=["1", "1", "y", "boss@example.com", "Boss"], secrets=[KEY, "a-long-password-1", "a-long-password-1"])
        self.assertEqual(self.go(io), 0)
        env = self.env_file.read_text(encoding="utf-8")
        for line in ("HCP_MODE=live", f"HCP_API_KEY={KEY}", "GEOCODER=census", "ROUTER=none"):
            self.assertIn(line, env.splitlines())
        self.assertRegex(env, r"(?m)^SESSION_SECRET=\S{40,}$")
        self.assertNotIn(KEY, io.text)                                         # the key is never printed
        self.assertNotIn("a-long-password-1", io.text)
        with self.env.db.session() as c:
            self.assertEqual(count_demo(c), {"jobs": 0, "technicians": 0})
        self.assertEqual([u["email"] for u in self.env.q("SELECT email FROM users")], ["boss@example.com"])
        self.assertIn("Removed the demo data", io.text)
        self.assertIn("python scripts/live_check.py", io.text)
        self.assertEqual(self.env.fake.methods(), {"GET"})                     # one read to test the key, nothing else

    def test_a_rejected_key_changes_nothing(self):
        io = Script(asks=["y", "y"], secrets=["bad-1", "bad-2", "bad-3"])
        self.assertEqual(self.go(io), 1)
        self.assertFalse(self.env_file.exists())
        self.assertIn("Nothing was changed", io.text)
        with self.env.db.session() as c:
            self.assertGreater(count_demo(c)["jobs"], 20)                      # the demo data was not touched either
        self.assertNotIn("bad-1", io.text)

    def test_an_already_saved_key_can_be_kept(self):
        io = Script(asks=["y", "1", "1", "n"])                                  # keep key, census, no routes, keep demo login
        self.assertEqual(self.go(io, existing_key=KEY), 0)
        self.assertNotIn("Housecall Pro API key (hidden)", io.text)             # never asked for it again
        self.assertIn(f"HCP_API_KEY={KEY}", self.env_file.read_text(encoding="utf-8"))
        self.assertEqual([u["email"] for u in self.env.q("SELECT email FROM users")], ["admin@example.com"])

    def test_google_geocoding_needs_its_own_key_and_rules_out_mapbox_routes(self):
        io = Script(asks=["2", "2", "n"], secrets=[KEY, "google-maps-key"])
        self.assertEqual(self.go(io), 0)
        env = self.env_file.read_text(encoding="utf-8").splitlines()
        self.assertIn("GEOCODER=google", env)
        self.assertIn("MAPS_API_KEY=google-maps-key", env)
        self.assertIn("ROUTER=none", env)                                       # one maps key cannot serve Google and Mapbox
        self.assertIn("road routes stay off", io.text)

    def test_own_osrm_server_and_the_public_demo_server(self):
        io = Script(asks=["1", "3", "https://osrm.mycompany.example/", "n"], secrets=[KEY])
        self.assertEqual(self.go(io), 0)
        env = self.env_file.read_text(encoding="utf-8").splitlines()
        self.assertIn("ROUTER=osrm", env)
        self.assertIn("ROUTER_URL=https://osrm.mycompany.example", env)
        io = Script(asks=["1", "4", "n"], secrets=[KEY])
        self.assertEqual(self.go(io), 0)
        self.assertIn("ROUTER_URL=https://router.project-osrm.org", self.env_file.read_text(encoding="utf-8").splitlines())
        self.assertIn("NOT recommended with real customers", io.text)

    def test_mapbox_routes_with_the_census_geocoder_ask_for_a_token(self):
        io = Script(asks=["1", "2", "n"], secrets=[KEY, "pk.mapbox-token"])
        self.assertEqual(self.go(io), 0)
        env = self.env_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(("ROUTER=mapbox" in env, "MAPS_API_KEY=pk.mapbox-token" in env, "GEOCODER=census" in env), (True, True, True))

    def test_a_weak_login_is_not_created_and_the_demo_login_stays(self):
        io = Script(asks=["1", "1", "y", "boss@example.com", "Boss"], secrets=[KEY, "short", "short"])
        self.assertEqual(self.go(io), 0)
        self.assertEqual([u["email"] for u in self.env.q("SELECT email FROM users")], ["admin@example.com"])
        self.assertIn("create_user.py", io.text)


class LiveCheckTests(unittest.TestCase):
    def setUp(self):
        self.env = LiveEnv(page_size=50)
        self.lines = []

    def tearDown(self):
        self.env.close()

    def check(self, routes=None, **kw):
        return run_live_check(self.env.db, self.env.cfg, self.env.client(), MockGeocoder(), routes or RoadRoutes(None), now=NOW,
                              printer=lambda *a: self.lines.append(" ".join(map(str, a))), **kw)

    def set_up_technicians(self):
        """What Admin > Technicians does: skills, a home base, and routing on."""
        with self.env.db.session() as c:
            c.execute("UPDATE technicians SET active = 1, trade_skills = '[\"PLB\",\"HVAC\"]', home_address = 'Chandler AZ', "
                      "home_lat = 33.30, home_lng = -111.84")

    def verdicts(self, report):
        return {c["text"][:40]: c["level"] for c in report["checks"]}

    def test_a_ready_account_passes_and_only_reads(self):
        self.env.live_sync()
        self.set_up_technicians()
        rep = self.check(do_sync=False)
        self.assertEqual(rep["technicians"]["routable"], len(self.env.fake.dataset["employees"]))
        self.assertEqual(rep["slots"]["jobs_tested"], 5)
        self.assertGreater(rep["slots"]["with_options"], 0)
        self.assertEqual(rep["slots"]["rule_violations"], 0)
        self.assertEqual(rep["timezone"], "America/Phoenix")
        self.assertTrue(any("company time zone: America/Phoenix" in ln for ln in self.lines))
        self.assertIn(rep["overall"], ("PASS", "WARN"))
        self.assertFalse([c for c in rep["checks"] if c["level"] == "FAIL"])
        self.assertEqual(self.env.fake.methods(), {"GET"})

    def test_a_friday_evening_check_still_reaches_monday(self):
        """Run after the shift on a Friday, a 3-day look-ahead is Friday + a weekend with nobody working: the check
        must not call routing broken for that, so it looks a week ahead."""
        friday_evening = datetime(2026, 10, 3, 0, 30, tzinfo=timezone.utc)          # Fri 17:30 America/Phoenix
        self.env.live_sync()
        self.set_up_technicians()
        rep = run_live_check(self.env.db, self.env.cfg, self.env.client(), MockGeocoder(), RoadRoutes(None), now=friday_evening,
                             do_sync=False, printer=lambda *a: self.lines.append(" ".join(map(str, a))))
        self.assertEqual(rep["slots"]["days_searched"], 7)
        self.assertEqual(rep["slots"]["with_options"], rep["slots"]["jobs_tested"])
        self.assertGreater(rep["slots"]["jobs_tested"], 0)
        self.assertFalse([c for c in rep["checks"] if c["level"] == "FAIL"])

    def test_the_look_ahead_can_be_set_and_a_miss_explains_itself(self):
        friday_evening = datetime(2026, 10, 3, 0, 30, tzinfo=timezone.utc)
        self.env.live_sync()
        self.set_up_technicians()
        rep = run_live_check(self.env.db, self.env.cfg, self.env.client(), MockGeocoder(), RoadRoutes(None), now=friday_evening,
                             do_sync=False, days=2, printer=lambda *a: self.lines.append(" ".join(map(str, a))))
        self.assertEqual((rep["slots"]["days_searched"], rep["slots"]["with_options"]), (2, 0))
        self.assertTrue(any("no slot in 2 days" in ln and re.search(r"\(\d+\)$", ln) for ln in self.lines), self.lines)   # with why

    def test_it_syncs_first_when_asked(self):
        self.set_up_technicians()
        rep = self.check()                                                       # do_sync defaults on
        self.assertEqual(rep["sync"]["status"], "ok")
        self.assertEqual(rep["jobs"]["by_status"]["unscheduled"], sum(j["work_status"] == "unscheduled" for j in self.env.fake.dataset["jobs"]))

    def test_the_report_and_the_console_carry_no_customer_data(self):
        self.env.live_sync()
        self.set_up_technicians()
        rep = self.check(do_sync=False)
        blob = json.dumps(rep) + "\n".join(self.lines)
        customers = self.env.fake.customer_text()
        self.assertGreater(len(customers), 10)
        for text in customers:
            self.assertNotIn(text, blob)
        for j in self.env.fake.dataset["jobs"]:
            self.assertNotIn(j["id"], blob)                                       # not even job ids

    def test_no_technician_set_up_is_explained_not_a_crash(self):
        self.env.live_sync()
        rep = self.check(do_sync=False)
        self.assertEqual(rep["technicians"]["routable"], 0)
        self.assertEqual(rep["slots"]["jobs_tested"], 0)
        self.assertTrue(any("Admin > Technicians" in c["text"] and c["level"] == "WARN" for c in rep["checks"]))
        self.assertEqual(rep["overall"], "WARN")

    def test_a_rejected_key_fails_clearly(self):
        env = LiveEnv(key="WRONG-key-999")
        try:
            rep = run_live_check(env.db, env.cfg, env.client(), MockGeocoder(), RoadRoutes(None), now=NOW, printer=lambda *a: None)
            self.assertEqual(rep["overall"], "FAIL")
            self.assertNotIn("WRONG-key-999", json.dumps(rep))
            self.assertIn("401", rep["checks"][-1]["text"])
        finally:
            env.close()

    def test_nothing_loaded_is_a_failure(self):
        self.env.fake.dataset["jobs"].clear()
        rep = self.check()
        self.assertEqual(rep["overall"], "FAIL")
        self.assertTrue(any("No jobs came back" in c["text"] for c in rep["checks"]))

    def test_a_misplaced_pin_is_reported(self):
        self.env.live_sync()
        self.set_up_technicians()
        with self.env.db.session() as c:
            c.execute("UPDATE jobs SET lat = 40.7, lng = -74.0 WHERE work_status = 'unscheduled'")     # New York, techs are in Arizona
        rep = self.check(do_sync=False)
        self.assertGreater(rep["far_pins"], 0)
        self.assertTrue(any("miles from every technician" in c["text"] and c["level"] == "WARN" for c in rep["checks"]))

    def test_drive_times_are_compared_with_real_roads_and_a_speed_is_suggested(self):
        self.env.live_sync()
        self.set_up_technicians()
        travel = HaversineTravel.from_settings(DEFAULT_SETTINGS)

        class Roads:                                                              # real drives take `factor` times the estimate
            def __init__(self, factor):
                self.factor, self.name = factor, f"osrm-x{factor}"                 # a different name: roads are cached per provider

            def route(self, a, b):
                return {"seconds": travel.minutes(a, b) * 60 * self.factor, "meters": travel.miles(a, b) * 1609.344,
                        "polyline": encode_polyline([a, b])}

        rep = self.check(routes=RoadRoutes(Roads(2.0)), do_sync=False)
        d = rep["drive"]
        self.assertEqual((d["provider"], d["road"] > 0), ("osrm-x2.0", True))
        self.assertAlmostEqual(d["median_road_over_estimate"], 2.0, places=1)
        self.assertEqual(d["suggested_speed_mph"], 14)                            # 28 mph / 2
        self.assertTrue(any("too tight" in c["text"] and "about 14 mph" in c["text"] and c["level"] == "WARN" for c in rep["checks"]))
        rep = self.check(routes=RoadRoutes(Roads(1.05)), do_sync=False)
        self.assertNotIn("suggested_speed_mph", rep["drive"])
        self.assertTrue(any("close enough" in c["text"] and c["level"] == "PASS" for c in rep["checks"]))

    def test_an_unreachable_router_is_a_warning(self):
        self.env.live_sync()
        self.set_up_technicians()

        class Down:
            name = "osrm"

            def route(self, a, b):
                raise HttpError(0, "/route", "URLError")

        rep = self.check(routes=RoadRoutes(Down()), do_sync=False)
        self.assertEqual(rep["drive"]["road"], 0)
        self.assertTrue(any("not answering well" in c["text"] and c["level"] == "WARN" for c in rep["checks"]))

    def test_the_script_rehearses_on_demo_data(self):
        out = Path(tempfile.mkdtemp()) / "r.json"
        try:
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / "live_check.py"), "--mock", "--out", str(out)],
                               capture_output=True, text=True, cwd=str(ROOT), timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            rep = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual((rep["mode"], rep["slots"]["rule_violations"]), ("mock", 0))
            self.assertIn("REHEARSAL", r.stdout)
        finally:
            shutil.rmtree(out.parent, ignore_errors=True)

    def test_the_script_refuses_to_run_outside_live_mode(self):
        env = {k: v for k, v in os.environ.items() if k not in ("HCP_MODE", "HCP_API_KEY")}
        env["HCP_MODE"] = "mock"
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "live_check.py")], capture_output=True, text=True,
                           cwd=str(ROOT), timeout=60, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("go_live.py", r.stderr + r.stdout)


class CheckOptionTests(unittest.TestCase):
    TECH = {"id": "t", "shift_start": "08:00", "shift_end": "17:00", "work_days": [0, 1, 2, 3, 4], "trade_skills": ["PLB"],
            "max_jobs_per_day": 4}
    GOOD = {"date": "2026-10-02", "window_start_min": 480, "window_end_min": 720, "start_min": 500, "end_min": 560,
            "stops_in_day": 2, "position": 3}
    TODAY = date(2026, 10, 1)

    def broken(self, tech=None, trade="PLB", **change):
        o = {**self.GOOD, **change}
        return check_option(o, tech or self.TECH, trade, self.TODAY, 480, 30)

    def test_a_good_option_is_clean(self):
        self.assertEqual(self.broken(), [])

    def test_every_rule_is_checked(self):
        cases = [({"start_min": 730}, "arrival outside its own window"), ({"start_min": 470}, "arrival outside its own window"),
                 ({"window_start_min": 420}, "window outside the shift"), ({"window_end_min": 1100}, "window outside the shift"),
                 ({"end_min": 1030}, "job ends after the shift"), ({"date": "2026-10-03"}, "not a working day"),
                 ({"stops_in_day": 4, "position": 3}, "over the daily maximum"), ({"position": 5}, "impossible position in the route"),
                 ({"position": 0}, "impossible position in the route"),
                 ({"date": "2026-10-01", "start_min": 500}, "arrival sooner than the lead time allows")]
        for change, expected in cases:
            with self.subTest(change=change):
                self.assertIn(expected, self.broken(**change))
        self.assertIn("technician lacks the trade skill", self.broken(trade="HVAC"))
        self.assertEqual(self.broken(trade=""), [])                               # a job with no trade can go to anyone
        self.assertEqual(check_option(self.GOOD, None, "PLB", self.TODAY, 480, 30), ["suggested for a technician that does not exist"])


if __name__ == "__main__":
    unittest.main()
