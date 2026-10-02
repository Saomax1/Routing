"""Integration tests: HCP (mock) -> sync -> database -> dispatch view / slots. No network."""
import copy
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, date

from app.config import Config
from app.db import Database
from app.domain.warranty_parser import WarrantyJob, parse_warranty_job
from app.hcp.client import HCPClient, MockHCPClient
from app.hcp.fixtures import DEMO_TECH_SETUP, build_ahs_description
from app.hcp.http import HttpError
from app.hcp.normalize import canonical_work_status, classify_source, normalize_employee, normalize_job
from app.services import ai_fallback
from app.services.dispatch_view import build_dispatch, build_job_detail, compute_slots
from app.services.geocode import MockGeocoder, geocode_cached
from app.services.settings_store import get_settings, replace_durations, save_settings, get_durations
from app.services.sync import SyncService

NOW = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)   # Thu 08:00 America/Phoenix


class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(database_path=os.path.join(self.tmp, "t.db"))
        self.db = Database(self.cfg.database_path)
        self.hcp = MockHCPClient(now=NOW)
        self.geo = MockGeocoder()
        self.svc = SyncService(self.db, self.cfg, self.hcp, self.geo,
                               new_tech_defaults=lambda eid: DEMO_TECH_SETUP.get(eid))

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def q(self, sql, *args):
        with self.db.session() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.result = self.e.svc.run(NOW)

    def tearDown(self):
        self.e.close()

    def test_first_sync_loads_everything(self):
        ds = self.e.hcp.dataset["jobs"]
        self.assertEqual(self.result["status"], "ok")
        self.assertEqual(self.result["jobs_seen"], len(ds))
        self.assertEqual(self.result["errors"], 0)
        self.assertEqual(len(self.e.q("SELECT 1 FROM jobs")), len(ds))

    def test_technicians_created_with_demo_setup(self):
        techs = {t["hcp_employee_id"]: t for t in self.e.q("SELECT * FROM technicians")}
        self.assertEqual(set(techs), {e["id"] for e in self.e.hcp.dataset["employees"]})
        self.assertEqual(json.loads(techs["emp_demo_2"]["trade_skills"]), ["PLB", "HVAC"])
        self.assertIsNotNone(techs["emp_demo_1"]["home_lat"])          # home geocoded
        self.assertEqual(techs["emp_demo_5"]["active"], 0)             # inactive in HCP

    def test_warranty_parsing_and_classification(self):
        rows = {r["hcp_job_id"]: r for r in self.e.q("SELECT * FROM jobs")}
        w = {r["hcp_job_id"]: r for r in self.e.q("SELECT * FROM warranty_details")}
        by_prio = {}
        for jid, wr in w.items():
            by_prio.setdefault(wr["dispatch_priority"], []).append(jid)
        self.assertIn("Emergency", by_prio)
        self.assertIn("Expedited", by_prio)
        ahs = [j for j in rows.values() if j["source_category"] == "ahs"]
        self.assertTrue(len(ahs) >= 10)
        other = [j for j in rows.values() if j["source_category"] == "other_warranty"]
        self.assertEqual(len(other), 1)                                  # "Choice Home Warranty" job
        self.assertNotIn(other[0]["hcp_job_id"], w)                      # not AHS format -> no warranty_details
        direct = [j for j in rows.values() if j["source_category"] == "direct"]
        self.assertTrue(any(j["lead_source"] == "Google LSA" for j in direct))

    def test_trades_resolved(self):
        rows = self.e.q("SELECT trade_code, COUNT(*) n FROM jobs GROUP BY trade_code")
        trades = {r["trade_code"] for r in rows}
        self.assertTrue({"PLB", "HVAC"} <= trades)

    def test_missing_address_job_is_unmapped_with_warning(self):
        bad = [r for r in self.e.q("SELECT j.hcp_job_id, j.lat, j.geocode_status, w.parse_warnings FROM jobs j "
                                   "JOIN warranty_details w USING (hcp_job_id) WHERE j.lat IS NULL")]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["geocode_status"], "failed")
        self.assertIn("address", bad[0]["parse_warnings"].lower())

    def test_second_sync_is_idempotent(self):
        r2 = self.e.svc.run(NOW)
        self.assertEqual(r2["jobs_changed"], 0)
        self.assertEqual(r2["status"], "ok")

    def test_changed_description_is_reparsed(self):
        j = next(x for x in self.e.hcp.dataset["jobs"] if x["work_status"] == "unscheduled"
                 and "ahs:92" in x["description"] and "Dispatch Priority:**Normal" in x["description"])
        jid = j["id"]
        before = self.e.q("SELECT dispatch_priority FROM warranty_details WHERE hcp_job_id = ?", jid)[0]["dispatch_priority"]
        self.assertEqual(before, "Normal")
        j["description"] = j["description"].replace("Dispatch Priority:**Normal", "Dispatch Priority:**Emergency")
        r = self.e.svc.run(NOW)
        self.assertGreaterEqual(r["jobs_changed"], 1)
        after = self.e.q("SELECT dispatch_priority FROM warranty_details WHERE hcp_job_id = ?", jid)[0]["dispatch_priority"]
        self.assertEqual(after, "Emergency")

    def test_job_removed_from_hcp_is_deactivated(self):
        victim = next(x for x in self.e.hcp.dataset["jobs"] if x["work_status"] == "unscheduled")
        self.e.hcp.dataset["jobs"].remove(victim)
        self.e.svc.run(NOW)
        self.assertEqual(self.e.q("SELECT active FROM jobs WHERE hcp_job_id = ?", victim["id"])[0]["active"], 0)

    def test_hcp_failure_keeps_old_data_and_records_error(self):
        n_before = len(self.e.q("SELECT 1 FROM jobs"))

        def boom():
            raise HttpError(401, "/jobs")
        self.e.hcp.list_unscheduled = boom
        r = self.e.svc.run(NOW)
        self.assertEqual(r["status"], "error")
        self.assertIn("401", r["error"])
        self.assertEqual(len(self.e.q("SELECT 1 FROM jobs WHERE active = 1")), n_before)
        self.assertEqual(self.e.q("SELECT status FROM sync_runs ORDER BY id DESC LIMIT 1")[0]["status"], "error")

    def test_one_bad_record_does_not_abort_the_run(self):
        original = self.e.svc._upsert_job
        victim = self.e.hcp.dataset["jobs"][0]["id"]

        def flaky(conn, n, settings, now):
            if n["hcp_job_id"] == victim:
                raise RuntimeError("boom")
            return original(conn, n, settings, now)
        self.e.svc._upsert_job = flaky
        r = self.e.svc.run(NOW)
        self.assertEqual(r["status"], "partial")
        self.assertEqual(r["errors"], 1)

    def test_concurrent_run_is_refused(self):
        self.e.svc._lock.acquire()
        try:
            self.assertEqual(self.e.svc.run(NOW), {"status": "busy"})
        finally:
            self.e.svc._lock.release()


class DispatchViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = Env()
        cls.e.svc.run(NOW)
        with cls.e.db.session() as c:
            cls.settings = get_settings(c)
            cls.view = build_dispatch(c, date(2026, 10, 1), cls.settings, NOW)

    @classmethod
    def tearDownClass(cls):
        cls.e.close()

    def test_unscheduled_sorted_by_score_emergency_first(self):
        u = self.view["unscheduled"]
        scores = [x["score"] for x in u]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(u[0]["priority_label"], "Emergency")
        self.assertEqual(u[0]["deadline_status"] in ("critical", "overdue", "warning"), True)

    def test_tech_routes_have_ordered_stops(self):
        techs = {t["id"]: t for t in self.view["technicians"]}
        self.assertNotIn("emp_demo_5", techs)                  # disabled tech with no work today is hidden
        for t in techs.values():
            starts = [s["start_min"] for s in t["stops"]]
            self.assertEqual(starts, sorted(starts))
            self.assertEqual([s["seq"] for s in t["stops"]], list(range(1, len(starts) + 1)))
        self.assertTrue(sum(t["job_count"] for t in techs.values()) > 0)
        self.assertTrue(all("drive_min" in t for t in techs.values()))

    def test_stats(self):
        self.assertEqual(self.view["stats"]["unmapped"], 1)
        self.assertEqual(self.view["stats"]["unscheduled"], len(self.view["unscheduled"]))
        self.assertEqual(self.view["today"], "2026-10-01")

    def test_no_phone_numbers_in_queue_payload(self):
        blob = json.dumps(self.view["unscheduled"])
        self.assertNotIn("tel:", blob)
        self.assertNotIn("4805550", blob)                      # phones are only in the detail endpoint

    def test_job_detail(self):
        with self.e.db.session() as c:
            top = self.view["unscheduled"][0]["id"]
            d = build_job_detail(c, top, self.settings, NOW)
            self.assertEqual(d["warranty"]["dispatch_priority"], "Emergency")
            self.assertTrue(d["warranty"]["do_not_collect_service_fee"])
            self.assertTrue(d["contact_phones"])
            self.assertTrue(d["score"]["breakdown"])
            self.assertTrue(d["warranty"]["authorization_link"].startswith("https://"))
            self.assertIsNone(build_job_detail(c, "nope", self.settings, NOW))

    def test_slots_for_emergency_plumbing_job(self):
        with self.e.db.session() as c:
            top = self.view["unscheduled"][0]["id"]
            res = compute_slots(c, top, self.settings, NOW)
        self.assertTrue(res["options"], res)
        self.assertTrue(res["duration_min"] >= 60)
        plb_techs = {"emp_demo_1", "emp_demo_2", "emp_demo_4"}
        for o in res["options"]:
            self.assertIn(o["tech_id"], plb_techs)             # Casey is HVAC only
        costs = [o["cost"] for o in res["options"]]
        self.assertEqual(costs, sorted(costs))
        self.assertTrue(any(i["tech_id"] == "emp_demo_3" and "PLB" in i["reason"] for i in res["ineligible"]))

    def test_slots_for_hvac_job_only_hvac_techs(self):
        hvac = next(u for u in self.view["unscheduled"] if u["trade_code"] == "HVAC" and u["lat"] is not None)
        with self.e.db.session() as c:
            res = compute_slots(c, hvac["id"], self.settings, NOW, search_days=5)
        self.assertTrue({o["tech_id"] for o in res["options"]} <= {"emp_demo_2", "emp_demo_3"})

    def test_slots_for_unmapped_job_explains_why(self):
        bad = next(u for u in self.view["unscheduled"] if u["lat"] is None)
        with self.e.db.session() as c:
            res = compute_slots(c, bad["id"], self.settings, NOW)
        self.assertEqual(res["options"], [])
        self.assertTrue(res["notes"])

    def test_slots_rejected_for_scheduled_job(self):
        with self.e.db.session() as c:
            jid = c.execute("SELECT hcp_job_id FROM jobs WHERE work_status='scheduled' LIMIT 1").fetchone()[0]
            with self.assertRaises(ValueError):
                compute_slots(c, jid, self.settings, NOW)


class SettingsTests(unittest.TestCase):
    def test_settings_roundtrip_and_durations(self):
        e = Env()
        try:
            with e.db.session() as c:
                s = save_settings(c, {"scoring": {"base_direct_lead": 44}, "deadline_rules": {"AHS": {"Normal": 48}}})
                self.assertEqual(s["scoring"]["base_direct_lead"], 44)
                self.assertEqual(s["scoring"]["base_by_priority"]["Emergency"], 100)   # untouched
                self.assertEqual(s["deadline_rules"], {"AHS": {"Normal": 48}})          # free-form map replaced
                self.assertEqual(get_settings(c)["scoring"]["base_direct_lead"], 44)
                with self.assertRaises(ValueError):
                    save_settings(c, {"scoring": {"base_direct_lead": "lots"}})
                d = replace_durations(c, [{"trade_code": "plb", "keyword": "Water Heater", "minutes": 150}])
                self.assertEqual(d, [{"trade_code": "PLB", "keyword": "water heater", "minutes": 150}])
                with self.assertRaises(ValueError):
                    replace_durations(c, [{"trade_code": "PLB", "keyword": "x", "minutes": 1}])
        finally:
            e.close()


class GeocodeTests(unittest.TestCase):
    class Counting:
        name = "counting"

        def __init__(self, result):
            self.result, self.calls = result, 0

        def geocode(self, a):
            self.calls += 1
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    def test_cache_hit_failure_cache_and_transient_errors(self):
        e = Env()
        try:
            with e.db.session() as c:
                p = self.Counting((33.3, -111.8))
                self.assertEqual(geocode_cached(c, p, "123 W Example St, Chandler, AZ 85226"), (33.3, -111.8, "ok"))
                geocode_cached(c, p, "123  w EXAMPLE st, chandler, az 85226")        # same normalised key
                self.assertEqual(p.calls, 1)

                bad = self.Counting(None)
                self.assertEqual(geocode_cached(c, bad, "nowhere land")[2], "failed")
                geocode_cached(c, bad, "nowhere land")
                self.assertEqual(bad.calls, 1)                                       # failure is cached 24h

                flaky = self.Counting(HttpError(503, "/x"))
                self.assertEqual(geocode_cached(c, flaky, "5 transient rd")[2], "error")
                geocode_cached(c, flaky, "5 transient rd")
                self.assertEqual(flaky.calls, 2)                                     # transient errors are retried
                self.assertEqual(geocode_cached(c, p, "")[2], "failed")
        finally:
            e.close()

    def test_mock_geocoder_is_deterministic_and_local(self):
        g = MockGeocoder()
        a = g.geocode("100 N Test St, Gilbert, AZ 85233")
        self.assertEqual(a, g.geocode("100 N Test St, Gilbert, AZ 85233"))
        self.assertTrue(33.2 < a[0] < 33.5 and -112.0 < a[1] < -111.6)
        self.assertIsNone(g.geocode("1 Main St, Springfield, IL 62701"))


class FakeTransport:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def request(self, method, url, headers=None, params=None, json_body=None):
        self.calls.append({"url": url, "headers": headers, "params": params})
        page = dict(params)["page"]
        return self.pages[page - 1] if page <= len(self.pages) else {"jobs": []}


class HCPClientTests(unittest.TestCase):
    def cfg(self):
        return Config(hcp_mode="live", hcp_api_key="sekret-key-123", hcp_page_size=2)

    def test_pagination_auth_header_and_params(self):
        t = FakeTransport([{"jobs": [{"id": "1"}, {"id": "2"}], "total_pages": 2},
                           {"jobs": [{"id": "3"}], "total_pages": 2}])
        c = HCPClient(self.cfg(), transport=t)
        jobs = c.list_unscheduled()
        self.assertEqual([j["id"] for j in jobs], ["1", "2", "3"])
        self.assertEqual(len(t.calls), 2)
        self.assertEqual(t.calls[0]["headers"]["Authorization"], "Token sekret-key-123")
        self.assertIn(("work_status[]", "unscheduled"), t.calls[0]["params"])

    def test_pagination_without_total_pages_stops_on_short_page(self):
        t = FakeTransport([{"jobs": [{"id": "1"}, {"id": "2"}]}, {"jobs": [{"id": "3"}]}])
        self.assertEqual(len(HCPClient(self.cfg(), transport=t).list_unscheduled()), 3)

    def test_list_response_shapes(self):
        t = FakeTransport([[{"id": "a"}]])           # bare list instead of {"jobs": [...]}
        self.assertEqual(len(HCPClient(self.cfg(), transport=t).list_unscheduled()), 1)

    def test_key_never_in_repr_and_writes_disabled(self):
        c = HCPClient(self.cfg(), transport=FakeTransport([]))
        self.assertNotIn("sekret", repr(c))
        self.assertNotIn("sekret", repr(self.cfg()))
        with self.assertRaises(NotImplementedError):
            c.set_schedule("job", None)
        with self.assertRaises(ValueError):
            HCPClient(Config(hcp_mode="live", hcp_api_key=""))


class NormalizeTests(unittest.TestCase):
    def test_tolerates_alternate_shapes(self):
        n = normalize_job({"id": 77, "status": "Needs Scheduling", "scheduled_start": "2026-10-02T15:00:00Z",
                           "notes": [{"content": "line one"}, {"content": "line two"}],
                           "customer": {"name": "Acme Co"}, "address": {"postal_code": "85225", "lat": "33.3", "lng": "-111.8"},
                           "assigned_employee_ids": ["e1"], "tags": [{"name": "warranty"}, "x"],
                           "lead_source": {"name": "Yelp"}, "job_type": {"name": "Plumbing"}})
        self.assertEqual(n["hcp_job_id"], "77")
        self.assertEqual(n["work_status"], "unscheduled")
        self.assertEqual(n["description_raw"], "line one\nline two")
        self.assertEqual(n["customer_name"], "Acme Co")
        self.assertEqual((n["hcp_lat"], n["hcp_lng"]), (33.3, -111.8))
        self.assertEqual(n["assigned_employee_ids"], ["e1"])
        self.assertEqual(n["tags"], ["warranty", "x"])
        self.assertEqual((n["lead_source"], n["job_type"], n["zip"]), ("Yelp", "Plumbing", "85225"))

    def test_garbage_does_not_crash(self):
        for raw in ({}, {"id": "x", "address": "123 Main", "customer": None, "schedule": "soon", "tags": None}):
            self.assertIsInstance(normalize_job(raw), dict)

    def test_status_and_source_helpers(self):
        self.assertEqual(canonical_work_status("in progress"), "in_progress")
        self.assertEqual(canonical_work_status("complete rated"), "complete")
        self.assertEqual(canonical_work_status("pro canceled"), "canceled")
        self.assertEqual(classify_source("", ["AHS"], None), "ahs")
        self.assertEqual(classify_source("Choice Home Warranty", [], None), "other_warranty")
        self.assertEqual(classify_source("Google LSA", ["lead"], None), "direct")
        self.assertEqual(normalize_employee({"id": "e", "first_name": "A", "last_name": "B"})["name"], "A B")


class AIFallbackTests(unittest.TestCase):
    class Fake:
        def __init__(self, text):
            self.text, self.sent = text, None

        def request(self, method, url, headers=None, params=None, json_body=None):
            self.sent = json_body
            return {"content": [{"type": "text", "text": self.text}]}

    def cfg(self, on=True):
        return Config(llm_api_key="k" if on else "", llm_model="some-model" if on else "")

    def test_redaction_removes_names_and_phones_but_keeps_address(self):
        text = build_ahs_description(name="JANE SAMPLE", phone="4805550101")
        red = ai_fallback.redact(text)
        self.assertNotIn("JANE SAMPLE", red)
        self.assertNotIn("555-0101", red)
        self.assertNotIn("4805550101", red)
        self.assertIn("123 W EXAMPLE ST", red)

    def test_fills_only_missing_fields_and_marks_ai(self):
        desc = build_ahs_description(include_address=False, priority="Expedited")
        job = parse_warranty_job(desc)
        self.assertIsNone(job.full_address)
        t = self.Fake('Sure! {"street": "9 W Fake Rd", "city": "Mesa", "state": "az", "zip_code": "85201", '
                      '"dispatch_priority": "Emergency"}')
        self.assertTrue(ai_fallback.ai_fill(self.cfg(), desc, job, t))
        self.assertEqual(job.full_address, "9 W Fake Rd, Mesa, AZ 85201")
        self.assertEqual(job.dispatch_priority, "Expedited")           # regex value is never overridden
        self.assertEqual(job.parsed_by, "ai")
        self.assertTrue(any("filled by AI" in w for w in job.parse_warnings))
        sent = json.dumps(t.sent)
        self.assertNotIn("JANE SAMPLE", sent)
        self.assertNotIn("4805550101", sent)

    def test_disabled_without_key_or_when_nothing_missing(self):
        desc = build_ahs_description(include_address=False)
        self.assertFalse(ai_fallback.ai_fill(self.cfg(on=False), desc, parse_warranty_job(desc), self.Fake("{}")))
        full = build_ahs_description()
        t = self.Fake("{}")
        self.assertFalse(ai_fallback.ai_fill(self.cfg(), full, parse_warranty_job(full), t))
        self.assertIsNone(t.sent)                                       # nothing missing -> no call at all

    def test_bad_ai_output_is_ignored(self):
        desc = build_ahs_description(include_address=False)
        job = parse_warranty_job(desc)
        self.assertFalse(ai_fallback.ai_fill(self.cfg(), desc, job, self.Fake("I cannot help with that")))
        self.assertFalse(ai_fallback.ai_fill(self.cfg(), desc, job, self.Fake('{"zip_code": "ABCDE", "state": "Arizona"}')))
        self.assertIsNone(job.full_address)


if __name__ == "__main__":
    unittest.main()
