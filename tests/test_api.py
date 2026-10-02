"""API tests: auth, roles, CSRF, validation, and the happy paths (mock HCP, no network)."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from app import api as api_module
from app.config import Config
from app.main import bootstrap_users, create_app
from tests.asgi_client import Client

ADMIN = ("boss@example.com", "correct-horse-battery")
DISPATCH = ("dispatch@example.com", "another-long-password")


def make_app(**cfg_kw):
    tmp = tempfile.mkdtemp()
    cfg = Config(database_path=os.path.join(tmp, "api.db"), bootstrap_admin_email=ADMIN[0],
                 bootstrap_admin_password=ADMIN[1], session_secret="test-secret", sync_on_startup=False, **cfg_kw)
    app = create_app(cfg, background_sync=False)
    bootstrap_users(app.state.db, cfg)
    return app, tmp


def login(app, creds):
    c = Client(app)
    r = c.post("/api/auth/login", {"email": creds[0], "password": creds[1]})
    assert r.status == 200, r.text
    return c


class ApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.tmp = make_app()
        cls.app.state.sync.run()                      # load the demo data once
        cls.admin = login(cls.app, ADMIN)
        r = cls.admin.post("/api/users", {"email": DISPATCH[0], "password": DISPATCH[1], "role": "dispatcher", "name": "Dee"})
        assert r.status == 201, r.text
        cls.disp = login(cls.app, DISPATCH)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class AuthTests(ApiBase):
    def test_public_health_and_static_index(self):
        c = Client(self.app)
        r = c.get("/api/health")
        self.assertEqual((r.status, r.json()["ok"]), (200, True))
        idx = c.get("/")
        self.assertEqual(idx.status, 200)
        self.assertIn("<title>", idx.text)

    def test_security_headers(self):
        h = Client(self.app).get("/api/health").headers
        self.assertIn("default-src 'self'", h["content-security-policy"])
        self.assertEqual(h["x-frame-options"], "DENY")
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertEqual(h["cache-control"], "no-store")

    def test_protected_routes_require_login(self):
        c = Client(self.app)
        for path in ("/api/dispatch", "/api/jobs/x", "/api/technicians", "/api/settings", "/api/sync/status",
                     "/api/auth/me", "/api/config", "/api/users", "/api/parse-review"):
            self.assertEqual(c.get(path).status, 401, path)
        self.assertEqual(c.post("/api/sync/run").status, 401)

    def test_csrf_header_required_for_mutations(self):
        c = Client(self.app, csrf=False)
        r = c.post("/api/auth/login", {"email": ADMIN[0], "password": ADMIN[1]})
        self.assertEqual(r.status, 403)

    def test_bad_login_is_generic_and_good_login_sets_session(self):
        c = Client(self.app)
        for creds in ((ADMIN[0], "wrong-password-1"), ("nobody@example.com", "whatever-password")):
            r = c.post("/api/auth/login", {"email": creds[0], "password": creds[1]})
            self.assertEqual((r.status, r.json()["error"]), (401, "Invalid email or password"))
        self.assertEqual(c.get("/api/auth/me").status, 401)
        self.assertEqual(c.post("/api/auth/login", {"email": ADMIN[0].upper(), "password": ADMIN[1]}).status, 200)
        me = c.get("/api/auth/me").json()["user"]
        self.assertEqual((me["email"], me["role"]), (ADMIN[0], "admin"))
        self.assertNotIn("password", str(me))

    def test_logout(self):
        c = login(self.app, DISPATCH)
        self.assertEqual(c.get("/api/auth/me").status, 200)
        c.post("/api/auth/logout")
        self.assertEqual(c.get("/api/auth/me").status, 401)

    def test_login_rate_limit(self):
        app, tmp = make_app()
        try:
            c = Client(app)
            codes = [c.post("/api/auth/login", {"email": ADMIN[0], "password": f"bad-password-{i}"}).status for i in range(7)]
            self.assertEqual(codes[:5], [401] * 5)
            self.assertEqual(codes[5], 429)
            # even the right password is refused while blocked
            self.assertEqual(c.post("/api/auth/login", {"email": ADMIN[0], "password": ADMIN[1]}).status, 429)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_malformed_bodies(self):
        c = Client(self.app)
        r = c.request("POST", "/api/auth/login", headers={"content-type": "application/json"})
        self.assertEqual(r.status, 400)

    def test_dispatcher_cannot_do_admin_things(self):
        d = self.disp
        self.assertEqual(d.put("/api/settings", {"settings": {"scoring": {"base_direct_lead": 1}}}).status, 403)
        self.assertEqual(d.put("/api/technicians/emp_demo_1", {"active": False}).status, 403)
        self.assertEqual(d.get("/api/users").status, 403)
        self.assertEqual(d.post("/api/users", {"email": "x@y.zz", "password": "long-enough-pw", "role": "admin"}).status, 403)


class DispatchApiTests(ApiBase):
    def test_config(self):
        cfg = self.disp.get("/api/config").json()
        self.assertEqual(cfg["mode"], "mock")
        self.assertTrue(cfg["map"]["tile_url"].startswith("https://"))
        self.assertNotIn("api_key", str(cfg).lower())

    def test_dispatch_view(self):
        r = self.disp.get("/api/dispatch")
        self.assertEqual(r.status, 200)
        d = r.json()
        self.assertTrue(len(d["unscheduled"]) >= 10)
        self.assertEqual(d["unscheduled"][0]["priority_label"], "Emergency")
        self.assertIn("technicians", d)
        self.assertEqual(self.disp.get(f"/api/dispatch?date={d['today']}").status, 200)
        self.assertEqual(self.disp.get("/api/dispatch?date=1999-01-01").status, 400)
        self.assertEqual(self.disp.get("/api/dispatch?date=not-a-date").status, 200)   # falls back to today

    def test_job_detail_and_404(self):
        top = self.disp.get("/api/dispatch").json()["unscheduled"][0]["id"]
        d = self.disp.get(f"/api/jobs/{top}").json()
        self.assertEqual(d["id"], top)
        self.assertTrue(d["score"]["breakdown"])
        self.assertTrue(d["hcp_url"].endswith(top))
        self.assertEqual(self.disp.get("/api/jobs/does-not-exist").status, 404)

    def test_slots_endpoint(self):
        top = self.disp.get("/api/dispatch").json()["unscheduled"][0]["id"]
        r = self.disp.post(f"/api/jobs/{top}/slots", {"days": 7})
        self.assertEqual(r.status, 200)
        res = r.json()
        self.assertTrue(res["options"], res)
        o = res["options"][0]
        for k in ("tech_id", "date", "start_iso", "added_drive_min", "route_preview", "position"):
            self.assertIn(k, o)
        self.assertEqual(self.disp.post(f"/api/jobs/{top}/slots", {"days": 99}).status, 400)
        self.assertEqual(self.disp.post("/api/jobs/nope/slots", {}).status, 404)
        sched = next(j for j in self.app.state.hcp.dataset["jobs"] if j["work_status"] == "scheduled")["id"]
        self.assertEqual(self.disp.post(f"/api/jobs/{sched}/slots", {}).status, 400)

    def test_sync_status_and_manual_sync(self):
        s = self.disp.get("/api/sync/status").json()
        self.assertEqual(s["mode"], "mock")
        self.assertTrue(s["runs"])
        self.assertGreater(s["counts"]["jobs_active"], 20)
        self.assertEqual(s["counts"]["geocode_failed"], 1)
        r = self.disp.post("/api/sync/run")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_parse_review_and_mark_reviewed(self):
        items = self.disp.get("/api/parse-review").json()["items"]
        self.assertEqual(len(items), 1)                         # the AHS job with no address
        self.assertTrue(any("address" in w.lower() for w in items[0]["warnings"]))
        jid = items[0]["id"]
        self.assertEqual(self.disp.post(f"/api/parse-review/{jid}/reviewed").status, 200)
        self.assertEqual(self.disp.get("/api/parse-review").json()["items"], [])
        self.assertEqual(self.disp.post("/api/parse-review/nope/reviewed").status, 404)

    def test_unexpected_errors_do_not_leak_details(self):
        with mock.patch.object(api_module, "build_dispatch", side_effect=RuntimeError("secret-customer-data")):
            r = self.disp.get("/api/dispatch")
        self.assertEqual(r.status, 500)
        self.assertEqual(r.json(), {"error": "Internal error"})
        self.assertNotIn("secret", r.text)


class AdminApiTests(ApiBase):
    def test_technician_update_validation_and_geocoding(self):
        a = self.admin
        r = a.put("/api/technicians/emp_demo_1", {"trade_skills": ["plb", "hvac", "plb"], "shift_start": "07:30",
                                                  "shift_end": "16:00", "work_days": [0, 1, 2], "max_jobs_per_day": 4,
                                                  "color": "#112233", "home_address": "55 N Test Rd, Gilbert, AZ 85233"})
        self.assertEqual(r.status, 200, r.text)
        t = r.json()["technician"]
        self.assertEqual(t["trade_skills"], ["PLB", "HVAC"])
        self.assertEqual((t["shift_start"], t["max_jobs_per_day"], t["work_days"]), ("07:30", 4, [0, 1, 2]))
        self.assertEqual(r.json()["home_geocode"], "ok")
        self.assertIsNotNone(t["home_lat"])
        for bad in ({"shift_start": "7am"}, {"shift_end": "06:00"}, {"work_days": [9]}, {"max_jobs_per_day": 0},
                    {"color": "red"}, {"trade_skills": ["not a code!"]}, {"trade_skills": "PLB"}):
            self.assertEqual(a.put("/api/technicians/emp_demo_1", bad).status, 400, bad)
        self.assertEqual(a.put("/api/technicians/nope", {"active": True}).status, 404)
        a.put("/api/technicians/emp_demo_1", {"trade_skills": ["PLB"], "shift_start": "08:00", "shift_end": "17:00",
                                              "work_days": [0, 1, 2, 3, 4], "max_jobs_per_day": 6})

    def test_unfindable_home_address_reports_failure(self):
        r = self.admin.put("/api/technicians/emp_demo_4", {"home_address": "1 Main St, Springfield, IL 62701"})
        self.assertEqual(r.json()["home_geocode"], "failed")
        self.assertIsNone(r.json()["technician"]["home_lat"])
        self.admin.put("/api/technicians/emp_demo_4", {"home_address": "1200 S Mockingbird Rd, Mesa, AZ 85204"})

    def test_settings_update(self):
        a = self.admin
        r = a.put("/api/settings", {"settings": {"scoring": {"base_direct_lead": 33}}, "durations":
                                    [{"trade_code": "PLB", "keyword": "", "minutes": 50}]})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json()["settings"]["scoring"]["base_direct_lead"], 33)
        self.assertEqual(r.json()["durations"], [{"trade_code": "PLB", "keyword": "", "minutes": 50}])
        self.assertEqual(a.put("/api/settings", {"settings": {"scoring": {"base_direct_lead": "x"}}}).status, 400)
        self.assertEqual(a.put("/api/settings", {}).status, 400)
        self.assertEqual(self.disp.get("/api/settings").json()["settings"]["scoring"]["base_direct_lead"], 33)
        a.put("/api/settings", {"settings": {"scoring": {"base_direct_lead": 30}}, "durations": [
            {"trade_code": "*", "keyword": "", "minutes": 60}, {"trade_code": "PLB", "keyword": "", "minutes": 60},
            {"trade_code": "HVAC", "keyword": "", "minutes": 75}]})

    def test_user_management(self):
        a = self.admin
        self.assertEqual(a.post("/api/users", {"email": "bad", "password": "long-enough-pw"}).status, 400)
        self.assertEqual(a.post("/api/users", {"email": "a@b.cc", "password": "short"}).status, 400)
        self.assertEqual(a.post("/api/users", {"email": "a@b.cc", "password": "long-enough-pw", "role": "root"}).status, 400)
        self.assertEqual(a.post("/api/users", {"email": DISPATCH[0], "password": "long-enough-pw"}).status, 409)
        r = a.post("/api/users", {"email": "temp@example.com", "password": "long-enough-pw"})
        self.assertEqual((r.status, r.json()["user"]["role"]), (201, "dispatcher"))
        self.assertNotIn("hash", r.text)
        users = a.get("/api/users").json()["users"]
        me = next(u for u in users if u["email"] == ADMIN[0])
        self.assertEqual(a.delete(f"/api/users/{me['id']}").status, 400)           # can't delete yourself
        tmp = next(u for u in users if u["email"] == "temp@example.com")
        self.assertEqual(a.delete(f"/api/users/{tmp['id']}").status, 200)
        self.assertEqual(a.delete("/api/users/9999").status, 404)


if __name__ == "__main__":
    unittest.main()
