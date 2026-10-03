"""Integration tests: HCP (mock) -> sync -> database -> dispatch view / slots. No network."""
import copy
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, date
from unittest import mock

from app.config import Config
from app.db import Database
from app.domain.warranty_parser import WarrantyJob, parse_warranty_job
from zoneinfo import ZoneInfo

from app.hcp.client import HCPClient, MockHCPClient
from app.hcp.fixtures import DEMO_TECH_SETUP, build_ahs_description
from app.hcp.http import HttpError
from app.hcp.normalize import canonical_work_status, normalize_employee, normalize_job
from app.services import ai_fallback
from app.services.dispatch_view import build_areas, build_dispatch, build_job_detail, compute_slots, load_technicians
from app.services.job_exceptions import clear_exception, exception_map, set_exception
from app.services.geocode import MockGeocoder, geocode_cached
from app.services.settings_store import get_settings, replace_durations, save_settings, get_durations
from app.services.sync import SyncService

NOW = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)   # Thu 08:00 America/Phoenix


class Env:
    def __init__(self, now=NOW):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(database_path=os.path.join(self.tmp, "t.db"))
        self.db = Database(self.cfg.database_path)
        self.hcp = MockHCPClient(now=now)
        self.geo = MockGeocoder()
        self.svc = SyncService(self.db, self.cfg, self.hcp, self.geo,
                               new_tech_defaults=lambda eid: DEMO_TECH_SETUP.get(eid))

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def q(self, sql, *args):
        with self.db.session() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def water_leak_job(self):
        """The demo's Chandler water-leak call: its dispatch text says Emergency, its tag makes it Expedited."""
        return self.q("SELECT hcp_job_id FROM warranty_details WHERE dispatch_priority = 'Emergency'")[0]["hcp_job_id"]


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
        w = {r["hcp_job_id"]: r for r in self.e.q("SELECT * FROM warranty_details")}
        by_prio = {}
        for jid, wr in w.items():
            by_prio.setdefault(wr["dispatch_priority"], []).append(jid)
        self.assertIn("Emergency", by_prio)                              # the dispatch text still says what it says
        self.assertIn("Expedited", by_prio)
        self.assertEqual(len(self.e.q("SELECT 1 FROM jobs WHERE hcp_job_id LIKE 'job_demo_%' AND lead_source = 'Choice Home Warranty'")), 1)
        with self.e.db.session() as c:                                   # ...but a job's TYPE comes from its tags
            u = build_dispatch(c, date(2026, 10, 1), get_settings(c), NOW)["unscheduled"]
        self.assertEqual({t: sum(1 for x in u if x["type_label"] == t) for t in ("Expedited", "Normal", "Recall", "Retail")},
                         {"Expedited": 3, "Normal": 4, "Recall": 1, "Retail": 4})
        self.assertEqual(sum(x["ad_lead"] for x in u), 1)
        self.assertTrue(all(x["kind"] == ("retail" if x["type_label"] == "Retail" else "warranty") for x in u))

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

    def test_unscheduled_sorted_by_score_expedited_first(self):
        u = self.view["unscheduled"]
        scores = [x["score"] for x in u]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(u[0]["type_label"], "Expedited")

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
            top = c.execute("SELECT hcp_job_id FROM warranty_details WHERE dispatch_priority = 'Emergency'").fetchone()[0]
            d = build_job_detail(c, top, self.settings, NOW)
            self.assertEqual(d["warranty"]["dispatch_priority"], "Emergency")
            self.assertEqual((d["type"]["kind"], d["type"]["label"]), ("warranty", "Expedited"))     # its tag, not its text, decides
            self.assertTrue(d["warranty"]["do_not_collect_service_fee"])
            self.assertTrue(d["contact_phones"])
            self.assertTrue(d["score"]["breakdown"])
            self.assertTrue(d["warranty"]["authorization_link"].startswith("https://"))
            self.assertIsNone(build_job_detail(c, "nope", self.settings, NOW))

    def test_slots_for_the_water_leak_plumbing_job(self):
        with self.e.db.session() as c:
            top = self.e.water_leak_job()
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


class ExceptionTests(unittest.TestCase):
    """Dispatcher waives a job's deadline (the 48 h target is not a hard limit)."""

    def setUp(self):
        self.e = Env()
        self.e.svc.run(NOW)

    def tearDown(self):
        self.e.close()

    def view(self):
        with self.e.db.session() as c:
            return build_dispatch(c, date(2026, 10, 1), get_settings(c), NOW)

    def overdue_job(self):
        return next(u for u in self.view()["unscheduled"] if u["deadline_status"] == "overdue")

    def test_marking_a_job_excuses_it_and_it_survives_a_resync(self):
        before = self.view()
        job = next(u for u in before["unscheduled"] if u["deadline_status"] == "overdue" and u["type_label"] == "Normal")
        with self.e.db.session() as c:
            exc = set_exception(c, job["id"], "customer_unavailable", "  back   Monday ", None)
        self.assertEqual((exc["reason_label"], exc["note"]), ("Customer not available", "back Monday"))

        for _ in range(2):                                         # second pass: after another sync from HCP
            after = self.view()
            entry = next(u for u in after["unscheduled"] if u["id"] == job["id"])
            self.assertEqual(entry["deadline_status"], "excused")
            self.assertEqual(entry["exception_label"], "Customer not available")
            self.assertLess(entry["score"], job["score"])          # lost the overdue points
            self.assertEqual(after["stats"]["overdue"], before["stats"]["overdue"] - 1)
            self.e.svc.run(NOW)

        with self.e.db.session() as c:
            self.assertTrue(clear_exception(c, job["id"]))
            self.assertFalse(clear_exception(c, job["id"]))        # already gone
        again = next(u for u in self.view()["unscheduled"] if u["id"] == job["id"])
        self.assertEqual((again["deadline_status"], again["exception_label"]), ("overdue", None))

    def test_detail_and_slots_use_the_exception(self):
        job = self.overdue_job()
        with self.e.db.session() as c:
            set_exception(c, job["id"], "other", "owner on vacation", None)
            s = get_settings(c)
            d = build_job_detail(c, job["id"], s, NOW)
            slots = compute_slots(c, job["id"], s, NOW, 7)
        self.assertEqual(d["score"]["deadline_status"], "excused")
        self.assertEqual(d["score"]["exception"]["note"], "owner on vacation")
        self.assertTrue(any("Deadline waived" in n for n in slots["notes"]))
        self.assertFalse(any(o["misses_deadline"] for o in slots["options"]))

    def test_validation(self):
        job = self.overdue_job()["id"]
        sched = self.e.q("SELECT hcp_job_id FROM jobs WHERE work_status = 'scheduled' LIMIT 1")[0]["hcp_job_id"]
        with self.e.db.session() as c:
            for reason, note in (("nonsense", ""), (None, ""), (5, ""), ("other", ""), ("other", "   "),
                                 ("customer_unavailable", "x" * 301), ("customer_unavailable", 42)):
                with self.assertRaises(ValueError, msg=f"{reason!r} {note!r}"):
                    set_exception(c, job, reason, note, None)
            with self.assertRaises(ValueError):
                set_exception(c, sched, "customer_unavailable", "", None)       # only unscheduled jobs
            self.assertIsNone(set_exception(c, "nope", "customer_unavailable", "", None))
            self.assertEqual(exception_map(c), {})
            self.assertIsNotNone(set_exception(c, job, "customer_unavailable", None, None))   # note is optional here

    def test_set_by_shows_name_or_email_and_survives_user_deletion(self):
        job = self.overdue_job()["id"]
        with self.e.db.session() as c:
            c.execute("INSERT INTO users(id, email, name, password_hash, role, created_at) VALUES "
                      "(1, 'dee@example.com', 'Dee', 'x', 'dispatcher', 'now'), (2, 'sam@example.com', '', 'x', 'dispatcher', 'now')")
            self.assertEqual(set_exception(c, job, "customer_unavailable", "", 1)["set_by"], "Dee")
            self.assertEqual(set_exception(c, job, "customer_unavailable", "", 2)["set_by"], "sam@example.com")
            c.execute("DELETE FROM users WHERE id = 2")
            self.assertIsNone(exception_map(c)[job]["set_by"])      # note stays, author just becomes unknown


class AreaTotalsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = Env()
        cls.e.svc.run(NOW)

    @classmethod
    def tearDownClass(cls):
        cls.e.close()

    def areas(self, days=None, patch=None):
        with self.e.db.session() as c:
            s = get_settings(c)
            if patch:
                s = {**s, **patch}
            return build_areas(c, s, NOW, days)

    def test_running_totals_cover_every_unscheduled_call(self):
        res = self.areas()
        with self.e.db.session() as c:
            n = c.execute("SELECT COUNT(*) FROM jobs WHERE active = 1 AND work_status = 'unscheduled'").fetchone()[0]
        self.assertEqual(res["totals"]["unscheduled"], n)
        self.assertEqual(sum(a["count"] for a in res["areas"]), n)
        self.assertEqual(res["totals"]["areas"], len(res["areas"]))
        self.assertEqual(res["group_by"], "city")
        counts = [a["count"] for a in res["areas"]]
        self.assertEqual(counts, sorted(counts, reverse=True))
        for a in res["areas"]:
            self.assertEqual(sum(a["by_trade"].values()), a["count"])

    def test_area_keys_match_the_queue_entries(self):
        keys = {a["key"]: a["count"] for a in self.areas()["areas"]}
        u = self.queue_entries()
        self.assertEqual({k: sum(1 for x in u if x["area_key"] == k) for k in keys}, keys)

    def queue_entries(self):
        with self.e.db.session() as c:
            return build_dispatch(c, date(2026, 10, 1), get_settings(c), NOW)["unscheduled"]

    def test_earliest_opening_is_real_and_sorted(self):
        res = self.areas()
        self.assertGreater(res["totals"]["with_opening"], 0)
        for a in res["areas"]:
            if not a["earliest"]:
                continue
            self.assertGreaterEqual(a["earliest"]["date"], res["today"])
            order = [(t["date"], t["start_min"]) for t in a["techs"]]
            self.assertEqual(order, sorted(order))
            self.assertEqual(a["earliest"], a["techs"][0])
            self.assertLessEqual(a["earliest"]["eligible_jobs"], a["count"])

    def test_calls_without_a_map_location_are_counted_but_have_no_opening(self):
        res = self.areas()
        unmapped = next(a for a in res["areas"] if a["unlocated"])
        self.assertGreaterEqual(unmapped["count"], unmapped["unlocated"])
        self.assertEqual(unmapped["count"], unmapped["unlocated"])      # the demo's address-less job is alone in its area
        self.assertIsNone(unmapped["earliest"])
        self.assertEqual(unmapped["techs"], [])

    def test_trade_skill_is_respected(self):
        res = self.areas()
        hvac_only = next(a for a in res["areas"] if a["by_trade"] == {"HVAC": 1} and a["earliest"])
        tech_skills = {t["id"]: t["trade_skills"] for t in self.techs()}
        for t in hvac_only["techs"]:
            self.assertIn("HVAC", tech_skills[t["tech_id"]])

    def techs(self):
        with self.e.db.session() as c:
            return load_technicians(c)

    def test_grouping_by_zip(self):
        res = self.areas(patch={"areas": {"group_by": "zip"}})
        self.assertEqual(res["group_by"], "zip")
        mapped = [a for a in res["areas"] if a["key"] != "none"]          # the address-less demo job has no area
        self.assertTrue(mapped)
        self.assertTrue(all(a["key"].startswith("zip:") and a["label"].startswith("ZIP ") for a in mapped), mapped)
        self.assertEqual(sum(a["count"] for a in res["areas"]), self.areas()["totals"]["unscheduled"])

    def test_wider_window_never_finds_fewer_openings(self):
        one, seven = self.areas(1), self.areas(7)
        self.assertEqual((one["days"], seven["days"]), (1, 7))
        self.assertLessEqual(one["totals"]["with_opening"], seven["totals"]["with_opening"])
        by_key = {a["key"]: a for a in seven["areas"]}
        for a in one["areas"]:
            if a["earliest"]:
                self.assertEqual(by_key[a["key"]]["earliest"]["date"], a["earliest"]["date"])  # same soonest slot

    def test_no_routable_technicians_is_explained(self):
        e = Env()
        try:
            e.svc.run(NOW)
            with e.db.session() as c:
                c.execute("UPDATE technicians SET active = 0")
                res = build_areas(c, get_settings(c), NOW)
            self.assertTrue(res["notes"])
            self.assertEqual(res["totals"]["with_opening"], 0)
            self.assertGreater(res["totals"]["unscheduled"], 0)          # the running totals still work
        finally:
            e.close()

    def test_excused_and_overdue_counts(self):
        e = Env()
        try:
            e.svc.run(NOW)
            with e.db.session() as c:
                before = build_areas(c, get_settings(c), NOW)
                jid = next(u["id"] for u in build_dispatch(c, date(2026, 10, 1), get_settings(c), NOW)["unscheduled"]
                           if u["deadline_status"] == "overdue")
                set_exception(c, jid, "customer_unavailable", "", None)
                after = build_areas(c, get_settings(c), NOW)
            self.assertEqual(sum(a["overdue"] for a in after["areas"]), sum(a["overdue"] for a in before["areas"]) - 1)
            self.assertEqual(sum(a["excused"] for a in after["areas"]), 1)
        finally:
            e.close()


class CompletedJobsTests(unittest.TestCase):
    """Jobs HCP has marked complete are pulled in and marked complete here (they used to just vanish)."""

    def setUp(self):
        self.e = Env()
        self.e.svc.run(NOW)

    def tearDown(self):
        self.e.close()

    def complete(self):
        return self.e.q("SELECT * FROM jobs WHERE work_status = 'complete'")

    def test_the_previous_workdays_jobs_are_pulled_and_marked_complete(self):
        done = self.complete()
        self.assertEqual(len(done), 8)                              # two for each of the four active technicians
        for r in done:
            self.assertEqual(r["active"], 1)
            self.assertTrue(r["completed_at"].endswith("Z"), r["completed_at"])
            self.assertEqual(r["scheduled_start"][:10], "2026-09-30")
        self.assertEqual(self.e.q("SELECT COUNT(*) n FROM jobs WHERE work_status = 'complete' AND assigned_employee_ids = '[]'")[0]["n"], 0)

    def test_open_jobs_have_no_completion_time(self):
        self.assertEqual(self.e.q("SELECT COUNT(*) n FROM jobs WHERE work_status != 'complete' AND completed_at IS NOT NULL")[0]["n"], 0)

    def test_a_job_hcp_completes_flips_from_scheduled_and_stays_on_the_map(self):
        row = self.e.q("SELECT hcp_job_id FROM jobs WHERE work_status = 'scheduled' ORDER BY scheduled_start LIMIT 1")[0]
        raw = next(j for j in self.e.hcp.dataset["jobs"] if j["id"] == row["hcp_job_id"])
        raw["work_status"] = "complete rated"
        raw["work_timestamps"] = {"completed_at": "2026-10-01T17:05:00Z"}
        res = self.e.svc.run(NOW)
        after = self.e.q("SELECT * FROM jobs WHERE hcp_job_id = ?", row["hcp_job_id"])[0]
        self.assertEqual((after["work_status"], after["active"], after["completed_at"]),
                         ("complete", 1, "2026-10-01T17:05:00Z"))
        self.assertGreaterEqual(res["jobs_changed"], 1)

    def test_a_second_sync_changes_nothing(self):
        self.assertEqual(self.e.svc.run(NOW)["jobs_changed"], 0)

    def test_completed_jobs_that_hcp_stops_returning_stay_as_history(self):
        self.e.hcp.dataset["jobs"] = [j for j in self.e.hcp.dataset["jobs"] if "complete" not in j["work_status"]]
        self.e.svc.run(NOW)
        self.assertEqual(len(self.complete()), 8)
        self.assertTrue(all(r["active"] == 1 for r in self.complete()))

    def test_a_failing_completed_call_does_not_cost_us_the_rest_of_the_sync(self):
        self.e.hcp.dataset["jobs"].append(copy.deepcopy(next(j for j in self.e.hcp.dataset["jobs"] if j["work_status"] == "unscheduled")))
        self.e.hcp.dataset["jobs"][-1]["id"] = "job_new_after_failure"
        with mock.patch.object(self.e.hcp, "list_completed", side_effect=HttpError(400, "/jobs")):
            res = self.e.svc.run(NOW)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(self.e.q("SELECT 1 FROM jobs WHERE hcp_job_id = 'job_new_after_failure'")), 1)
        self.assertEqual(len(self.complete()), 8)                   # what we already knew is untouched
        last = self.e.q("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1")[0]
        self.assertIn("completed jobs could not be fetched", last["error"])

    def test_a_job_we_missed_while_the_completed_call_was_failing_is_picked_up_later(self):
        row = self.e.q("SELECT hcp_job_id FROM jobs WHERE work_status = 'scheduled' ORDER BY scheduled_start LIMIT 1")[0]
        raw = next(j for j in self.e.hcp.dataset["jobs"] if j["id"] == row["hcp_job_id"])
        raw["work_status"] = "complete unrated"
        with mock.patch.object(self.e.hcp, "list_completed", side_effect=HttpError(0, "/jobs", "URLError")):
            self.e.svc.run(NOW)                                     # HCP no longer lists it as scheduled: hidden for now
            self.assertEqual(self.e.q("SELECT active FROM jobs WHERE hcp_job_id = ?", row["hcp_job_id"])[0]["active"], 0)
        self.e.svc.run(NOW)                                         # the call works again: it comes back, as complete
        back = self.e.q("SELECT work_status, active FROM jobs WHERE hcp_job_id = ?", row["hcp_job_id"])[0]
        self.assertEqual((back["work_status"], back["active"]), ("complete", 1))

    def test_todays_completed_jobs_survive_a_sync_where_the_completed_call_fails(self):
        # Today's jobs are inside the window where unreturned jobs are hidden, so this is the case that matters:
        # HCP no longer lists a finished job as scheduled, and if the completed call fails it must not vanish.
        late = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)                  # 13:00: the morning's jobs are done
        e = Env(now=late)
        try:
            e.svc.run(late)
            todays = e.q("SELECT hcp_job_id FROM jobs WHERE work_status = 'complete' AND scheduled_start LIKE '2026-10-01%'")
            self.assertGreater(len(todays), 0)
            with mock.patch.object(e.hcp, "list_completed", side_effect=HttpError(0, "/jobs", "URLError")):
                e.svc.run(late)
            still = e.q("SELECT hcp_job_id, work_status, active FROM jobs WHERE work_status = 'complete' "
                        "AND scheduled_start LIKE '2026-10-01%'")
            self.assertEqual(len(still), len(todays))
            self.assertTrue(all(r["active"] == 1 for r in still))
        finally:
            e.close()

    def test_a_missing_completion_time_falls_back_to_the_last_update(self):
        for j in self.e.hcp.dataset["jobs"]:
            j.pop("work_timestamps", None)
        self.e.svc.run(NOW)
        self.assertTrue(all(r["completed_at"] for r in self.complete()))


class MigrationTests(unittest.TestCase):
    def test_a_database_from_before_completion_tracking_gets_the_new_column(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "old.db")
            Database(path)
            raw = sqlite3.connect(path)
            raw.execute("INSERT INTO jobs(hcp_job_id, work_status) VALUES ('keep-me', 'scheduled')")
            raw.execute("ALTER TABLE jobs DROP COLUMN completed_at")             # what an older release created
            raw.commit()
            self.assertNotIn("completed_at", [r[1] for r in raw.execute("PRAGMA table_info(jobs)")])
            raw.close()
            db = Database(path)                                                   # starting the app again migrates it
            with db.session() as c:
                self.assertIn("completed_at", [r["name"] for r in c.execute("PRAGMA table_info(jobs)")])
                self.assertEqual(c.execute("SELECT work_status, completed_at FROM jobs WHERE hcp_job_id = 'keep-me'").fetchone()[:], ("scheduled", None))
            Database(path)                                                        # and doing it twice is harmless
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CompletedJobsViewTests(unittest.TestCase):
    """At 1 PM on the demo Thursday the morning's jobs are done, as they would be in HCP."""
    LATE = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)        # 13:00 America/Phoenix

    @classmethod
    def setUpClass(cls):
        cls.e = Env(now=cls.LATE)
        cls.e.svc.run(cls.LATE)
        with cls.e.db.session() as c:
            cls.settings = get_settings(c)
            cls.view = build_dispatch(c, date(2026, 10, 1), cls.settings, cls.LATE)
            cls.yesterday = build_dispatch(c, date(2026, 9, 30), cls.settings, cls.LATE)

    @classmethod
    def tearDownClass(cls):
        cls.e.close()

    def stops(self, view):
        return [s for t in view["technicians"] for s in t["stops"]]

    def test_finished_and_in_progress_stops_are_marked(self):
        statuses = {s["status"] for s in self.stops(self.view)}
        self.assertIn("complete", statuses)
        self.assertIn("scheduled", statuses)
        done = [s for s in self.stops(self.view) if s["status"] == "complete"]
        self.assertTrue(all(s["completed_iso"] for s in done))

    def test_counts_split_open_and_done(self):
        total = sum(t["job_count"] for t in self.view["technicians"])
        done = sum(t["done_count"] for t in self.view["technicians"])
        self.assertGreater(done, 0)
        self.assertEqual(self.view["stats"]["completed_today"], done)
        self.assertEqual(self.view["stats"]["scheduled_today"], total - done)
        for t in self.view["technicians"]:
            self.assertEqual(t["done_count"], sum(1 for s in t["stops"] if s["status"] == "complete"))

    def test_the_previous_day_shows_its_finished_route(self):
        stops = self.stops(self.yesterday)
        self.assertEqual(len(stops), 8)
        self.assertTrue(all(s["status"] == "complete" for s in stops))
        self.assertEqual(self.yesterday["stats"]["completed_today"], 8)
        self.assertEqual(self.yesterday["stats"]["scheduled_today"], 0)

    def test_stops_carry_their_arrival_window(self):
        for s in self.stops(self.view):
            self.assertEqual(s["window_start_min"], s["start_min"])
            self.assertEqual(s["window_end_min"] - s["window_start_min"], 60)       # the demo jobs promise a 60-minute window
            self.assertEqual(s["window_minutes"], 60)

    def test_a_job_with_no_hcp_window_gets_the_standard_four_hours(self):
        with self.e.db.session() as c:
            c.execute("UPDATE jobs SET arrival_window_minutes = NULL")
            v = build_dispatch(c, date(2026, 10, 1), self.settings, self.LATE)
            c.rollback()
        self.assertTrue(all(s["window_minutes"] == 240 for s in self.stops(v)))

    def test_slots_start_from_the_last_completed_job_and_count_it(self):
        with self.e.db.session() as c:
            top = self.e.water_leak_job()
            res = compute_slots(c, top, self.settings, self.LATE, 1)
        kinds = {o["tech_id"]: o["origin"]["kind"] for o in res["options"]}
        self.assertIn("complete", set(kinds.values()))                              # someone has already worked today
        with_done = next(o for o in res["options"] if o["origin"]["kind"] == "complete")
        done_count = next(t["done_count"] for t in self.view["technicians"] if t["id"] == with_done["tech_id"])
        self.assertGreaterEqual(with_done["stops_in_day"], done_count)

    def test_a_window_can_be_chosen_per_search(self):
        with self.e.db.session() as c:
            top = self.e.water_leak_job()
            default = compute_slots(c, top, self.settings, self.LATE, 3)
            short = compute_slots(c, top, self.settings, self.LATE, 3, 120)
            self.assertEqual(default["window_minutes"], 240)
            self.assertEqual(short["window_minutes"], 120)
            self.assertTrue(all(o["window_minutes"] <= 120 for o in short["options"]))
            self.assertTrue(any(o["window_minutes"] == 240 for o in default["options"]))
            for bad in (0, 10, 721, 5000):
                with self.assertRaises(ValueError, msg=str(bad)):
                    compute_slots(c, top, self.settings, self.LATE, 3, bad)

    def test_every_window_holds_its_arrival_and_stays_in_the_shift(self):
        with self.e.db.session() as c:
            for u in self.view["unscheduled"][:6]:
                if u["lat"] is None:
                    continue
                for o in compute_slots(c, u["id"], self.settings, self.LATE, 5)["options"]:
                    self.assertTrue(o["window_start_min"] <= o["start_min"] <= o["window_end_min"], o)
                    self.assertLessEqual(o["window_end_min"], 17 * 60)
                    self.assertGreaterEqual(o["window_start_min"], 8 * 60)


class SettingsTests(unittest.TestCase):
    def test_settings_roundtrip_and_durations(self):
        e = Env()
        try:
            with e.db.session() as c:
                s = save_settings(c, {"scoring": {"base_retail": 44}, "deadline_rules": {"WARRANTY": {"Normal": 48}}})
                self.assertEqual(s["scoring"]["base_retail"], 44)
                self.assertEqual(s["scoring"]["base_by_priority"]["Expedited"], 60)    # untouched
                self.assertEqual(s["deadline_rules"], {"WARRANTY": {"Normal": 48}})     # free-form map replaced
                # ...and it stays replaced on the next read: removed rules must not reappear from the defaults
                self.assertEqual(get_settings(c)["deadline_rules"], {"WARRANTY": {"Normal": 48}})
                self.assertEqual(get_settings(c)["scoring"]["base_retail"], 44)
                with self.assertRaises(ValueError):
                    save_settings(c, {"scoring": {"base_retail": "lots"}})
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

    def test_scheduled_calls_include_work_in_progress_and_completed_calls_have_their_own_request(self):
        t = FakeTransport([{"jobs": []}])
        c = HCPClient(self.cfg(), transport=t)
        tz = ZoneInfo("America/Phoenix")
        c.list_scheduled(date(2026, 10, 1), date(2026, 10, 15), tz)
        scheduled = t.calls[-1]["params"]
        self.assertIn(("work_status[]", "scheduled"), scheduled)
        self.assertIn(("work_status[]", "in progress"), scheduled)
        self.assertNotIn(("work_status[]", "complete rated"), scheduled)
        c.list_completed(date(2026, 9, 28), date(2026, 10, 1), tz)
        done = t.calls[-1]["params"]
        self.assertIn(("work_status[]", "complete rated"), done)
        self.assertIn(("work_status[]", "complete unrated"), done)
        self.assertNotIn(("work_status[]", "scheduled"), done)
        self.assertIn(("scheduled_start_min", "2026-09-28T07:00:00Z"), done)           # midnight Phoenix = 07:00 UTC
        self.assertIn(("scheduled_start_max", "2026-10-02T07:00:00Z"), done)           # the end of that last day

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
        self.assertEqual(n["description_raw"], "")                   # ordinary private notes are not read at all
        self.assertEqual(n["customer_name"], "Acme Co")
        self.assertEqual((n["hcp_lat"], n["hcp_lng"]), (33.3, -111.8))
        self.assertEqual(n["assigned_employee_ids"], ["e1"])
        self.assertEqual(n["tags"], ["warranty", "x"])
        self.assertEqual((n["lead_source"], n["job_type"], n["zip"]), ("Yelp", "Plumbing", "85225"))

    def test_completion_time_and_arrival_window(self):
        done = normalize_job({"id": "d", "work_status": "complete rated",
                              "work_timestamps": {"completed_at": "2026-10-01T17:05:00Z"},
                              "schedule": {"scheduled_start": "2026-10-01T15:00:00Z", "arrival_window": 240}})
        self.assertEqual((done["work_status"], done["completed_at"], done["arrival_window_minutes"]),
                         ("complete", "2026-10-01T17:05:00Z", 240))
        self.assertEqual(normalize_job({"id": "e", "completed_at": "2026-10-01T10:00:00-07:00"})["completed_at"], "2026-10-01T17:00:00Z")
        open_job = normalize_job({"id": "o", "work_status": "scheduled"})
        self.assertIsNone(open_job["completed_at"])
        self.assertIsNone(open_job["arrival_window_minutes"])

    def test_garbage_does_not_crash(self):
        for raw in ({}, {"id": "x", "address": "123 Main", "customer": None, "schedule": "soon", "tags": None}):
            self.assertIsInstance(normalize_job(raw), dict)

    def test_status_and_employee_helpers(self):
        self.assertEqual(canonical_work_status("in progress"), "in_progress")
        self.assertEqual(canonical_work_status("complete rated"), "complete")
        self.assertEqual(canonical_work_status("pro canceled"), "canceled")
        self.assertEqual(normalize_employee({"id": "e", "first_name": "A", "last_name": "B"})["name"], "A B")


class MockHcpTests(unittest.TestCase):
    def test_the_demo_day_follows_the_clock(self):
        early = MockHCPClient(now=NOW)                                                   # 08:00: nothing has happened yet
        late = MockHCPClient(now=datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc))      # 13:00
        today = (date(2026, 10, 1), date(2026, 10, 1))
        tz = ZoneInfo("America/Phoenix")
        self.assertEqual({j["work_status"] for j in early.list_scheduled(*today, tz)}, {"scheduled"})
        self.assertEqual(early.list_completed(*today, tz), [])
        self.assertTrue(any(j["work_status"] == "complete rated" for j in late.dataset["jobs"]))
        statuses = {canonical_work_status(j["work_status"]) for j in late.list_scheduled(*today, tz)}
        self.assertTrue(statuses <= {"scheduled", "in_progress"}, statuses)
        self.assertTrue(late.list_completed(*today, tz))
        for j in late.list_completed(*today, tz):
            self.assertEqual(canonical_work_status(j["work_status"]), "complete")
            self.assertIn("completed_at", j["work_timestamps"])

    def test_yesterdays_completed_jobs_are_always_there_and_never_listed_as_scheduled(self):
        c = MockHCPClient(now=NOW)
        tz = ZoneInfo("America/Phoenix")
        wed = date(2026, 9, 30)
        self.assertEqual(len(c.list_completed(wed, wed, tz)), 8)
        self.assertEqual(c.list_scheduled(wed, wed, tz), [])
        self.assertEqual(len({j["id"] for j in c.dataset["jobs"]}), len(c.dataset["jobs"]))     # ids stay unique


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
