"""Confirming a suggested slot: the booking is checked again on the server, saved here, and joins the route."""
import sqlite3
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from app.services import bookings, confirm_slot as confirm_module
from app.services.confirm_slot import BookingConflict, confirm_slot
from app.services.dispatch_view import build_areas, build_dispatch, build_job_detail, compute_slots
from app.services.settings_store import get_settings
from tests.test_pipeline import NOW, Env

TODAY = date(2026, 10, 1)            # NOW is Thu 08:00 America/Phoenix


class BookingBase(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.e.svc.run(NOW)
        with self.e.db.session() as c:
            self.uid = c.execute("INSERT INTO users(email, name, password_hash, role, created_at) "
                                 "VALUES ('dee@example.com', 'Dee', 'x', 'dispatcher', '2026-01-01T00:00:00Z')").lastrowid
            self.settings = get_settings(c)
            self.top = build_dispatch(c, TODAY, self.settings, NOW)["unscheduled"]
        self.job = next(u["id"] for u in self.top if u["lat"] is not None)      # the Emergency plumbing call

    def tearDown(self):
        self.e.close()

    # -- helpers
    def slots(self, jid=None, now=NOW, **kw):
        with self.e.db.session() as c:
            return compute_slots(c, jid or self.job, self.settings, now, **kw)

    @staticmethod
    def pick(res, o):
        """What the browser sends: only which suggestion it saw."""
        return {"tech_id": o["tech_id"], "date": o["date"], "window_start_min": o["window_start_min"],
                "window_end_min": o["window_end_min"], "window_minutes": res["window_minutes"],
                "after_stop_id": o["after_stop_id"], "before_stop_id": o["before_stop_id"]}

    def book(self, sel, jid=None, now=NOW, user=None):
        with self.e.db.session() as c:
            return confirm_slot(c, jid or self.job, sel, now, self.uid if user is None else user)

    def view(self, d=TODAY, now=NOW):
        with self.e.db.session() as c:
            return build_dispatch(c, d, self.settings, now)

    def book_best(self, jid=None, index=0):
        res = self.slots(jid)
        o = res["options"][index]
        return res, o, self.book(self.pick(res, o), jid)["booking"]


class ConfirmTests(BookingBase):
    def test_confirm_books_the_suggested_slot(self):
        res, o, b = self.book_best()
        self.assertEqual((b["tech_id"], b["date"]), (o["tech_id"], o["date"]))
        self.assertEqual((b["arrive_iso"], b["end_iso"]), (o["start_iso"], o["end_iso"]))
        self.assertEqual((b["window_start_iso"], b["window_end_iso"]), (o["window_start_iso"], o["window_end_iso"]))
        self.assertEqual((b["window_start_min"], b["window_end_min"]), (o["window_start_min"], o["window_end_min"]))
        self.assertEqual((b["arrive_min"], b["duration_min"]), (o["start_min"], res["duration_min"]))
        self.assertEqual((b["booked_by"], b["booked_at"]), ("Dee", "2026-10-01T15:00:00Z"))

    def test_the_booked_job_joins_the_technicians_route(self):
        _, o, b = self.book_best()
        v = self.view(date.fromisoformat(o["date"]))
        tech = next(t for t in v["technicians"] if t["id"] == o["tech_id"])
        stop = next(s for s in tech["stops"] if s["id"] == self.job)
        self.assertTrue(stop["booked"])
        self.assertEqual((stop["status"], stop["booked_by"]), ("scheduled", "Dee"))
        self.assertEqual((stop["window_start_min"], stop["window_end_min"]), (o["window_start_min"], o["window_end_min"]))
        self.assertEqual((stop["start_min"], stop["end_min"]), (o["start_min"], o["end_min"]))
        self.assertEqual(stop["seq"], o["position"])                      # exactly where the dispatcher saw it
        self.assertEqual([s["seq"] for s in tech["stops"]], list(range(1, tech["job_count"] + 1)))
        self.assertFalse(any(s.get("booked") for t in v["technicians"] for s in t["stops"] if s["id"] != self.job))

    def test_it_leaves_the_queue_and_the_totals_follow(self):
        before = self.view()
        _, o, _ = self.book_best()
        after = self.view()
        self.assertNotIn(self.job, [u["id"] for u in after["unscheduled"]])
        self.assertEqual(after["stats"]["unscheduled"], before["stats"]["unscheduled"] - 1)
        self.assertEqual(before["bookings"], [])
        (listed,) = after["bookings"]
        self.assertEqual((listed["job_id"], listed["tech_id"]), (self.job, o["tech_id"]))
        self.assertTrue(listed["customer_name"] and listed["address"] and listed["hcp_url"].endswith(self.job))
        if o["date"] == TODAY.isoformat():
            self.assertEqual(after["stats"]["scheduled_today"], before["stats"]["scheduled_today"] + 1)
        with self.e.db.session() as c:
            areas = build_areas(c, self.settings, NOW)
        self.assertEqual(areas["totals"]["unscheduled"], before["stats"]["unscheduled"] - 1)

    def test_only_the_booked_day_shows_it(self):
        _, o, _ = self.book_best()
        other = date.fromisoformat(o["date"]) + timedelta(days=5)
        v = self.view(other)
        self.assertFalse(any(s["id"] == self.job for t in v["technicians"] for s in t["stops"]))

    def test_the_job_card_shows_the_booking_and_slots_are_refused(self):
        with self.e.db.session() as c:
            self.assertIsNone(build_job_detail(c, self.job, self.settings, NOW)["booking"])
        _, o, _ = self.book_best()
        with self.e.db.session() as c:
            d = build_job_detail(c, self.job, self.settings, NOW)
            self.assertEqual((d["booking"]["tech_id"], d["booking"]["date"]), (o["tech_id"], o["date"]))
            with self.assertRaises(ValueError):
                compute_slots(c, self.job, self.settings, NOW)

    def test_later_searches_plan_around_the_booking(self):
        res = self.slots()
        o = res["options"][0]
        # another plumbing call that could also have gone to that technician on that day
        key = (o["tech_id"], o["date"])
        others = [u["id"] for u in self.top if u["lat"] is not None and u["id"] != self.job and u["trade_code"] == "PLB"]
        other, before = next((j, x) for j in others for x in self.slots(j, limit=100)["options"] if (x["tech_id"], x["date"]) == key)
        self.book(self.pick(res, o))
        after = next(x for x in self.slots(other, limit=100)["options"] if (x["tech_id"], x["date"]) == key)
        self.assertEqual(before["stops_in_day"], o["stops_in_day"])
        self.assertEqual(after["stops_in_day"], o["stops_in_day"] + 1)    # the booked job counts as a stop of that day

    def test_any_listed_option_can_be_confirmed_not_just_the_top_few(self):
        """With many technicians the list is cut to the top 5; the check must still see the one that was picked."""
        with self.e.db.session() as c:
            src = c.execute("SELECT * FROM technicians WHERE hcp_employee_id = 'emp_demo_1'").fetchone()
            for i in range(7):
                c.execute("INSERT INTO technicians(hcp_employee_id, name, active, trade_skills, home_address, home_lat, "
                          "home_lng, color) VALUES (?,?,1,?,?,?,?,?)",
                          (f"extra_{i}", f"Extra {i}", src["trade_skills"], src["home_address"], src["home_lat"],
                           src["home_lng"] + i * 0.01, "#123456"))
        res = self.slots(limit=100)
        day = res["options"][0]["date"]
        ranked = [o for o in res["options"] if o["date"] == day]
        self.assertGreater(len(ranked), 5)
        last = ranked[-1]
        self.assertEqual(self.book(self.pick(res, last))["booking"]["tech_id"], last["tech_id"])

    def test_a_booking_is_ignored_the_moment_hcp_shows_the_job_scheduled(self):
        """Before any sync has tidied up: the job must not appear twice (HCP's stop and ours)."""
        _, o, _ = self.book_best()
        with self.e.db.session() as c:
            c.execute("UPDATE jobs SET work_status = 'scheduled', scheduled_start = ?, scheduled_end = ?, "
                      "assigned_employee_ids = ? WHERE hcp_job_id = ?",
                      (o["start_iso"], o["end_iso"], '["%s"]' % o["tech_id"], self.job))
        v = self.view(date.fromisoformat(o["date"]))
        stops = [s for t in v["technicians"] for s in t["stops"] if s["id"] == self.job]
        self.assertEqual([s["booked"] for s in stops], [False])
        self.assertEqual(v["bookings"], [])

    def test_the_booking_is_between_the_stops_the_dispatcher_saw(self):
        """Window and arrival can disagree about order (a 1-5 PM window, arriving 3:10 after a 1:30 stop): the route
        must keep the order that was confirmed."""
        res = self.slots()
        o = next(x for x in res["options"] if x["after_stop_id"] and x["before_stop_id"] is None)   # last of the day
        prev = o["after_stop_id"]
        self.book(self.pick(res, o))
        v = self.view(date.fromisoformat(o["date"]))
        stops = next(t for t in v["technicians"] if t["id"] == o["tech_id"])["stops"]
        ids = [s["id"] for s in stops]
        self.assertEqual(ids[ids.index(prev) + 1], self.job)

    def test_confirming_does_not_trust_the_browser_for_times(self):
        res = self.slots()
        o = res["options"][0]
        sel = {**self.pick(res, o), "start_iso": "2031-01-01T00:00:00Z", "arrive_at": "2031-01-01T00:00:00Z",
               "end_iso": "2031-01-01T01:00:00Z", "duration_min": 1, "arrive_min": 1}
        b = self.book(sel)["booking"]
        self.assertEqual((b["arrive_iso"], b["end_iso"], b["duration_min"]), (o["start_iso"], o["end_iso"], res["duration_min"]))

    def test_a_note_is_kept_and_tidied(self):
        res = self.slots()
        b = self.book({**self.pick(res, res["options"][0]), "note": "  prefers   mornings\n"})["booking"]
        self.assertEqual(b["note"], "prefers mornings")

    def test_the_action_is_logged(self):
        _, o, _ = self.book_best()
        (row,) = self.e.q("SELECT * FROM schedule_actions")
        self.assertEqual((row["hcp_job_id"], row["user_id"], row["new_tech"], row["new_start"], row["success"]),
                         (self.job, self.uid, o["tech_id"], o["start_iso"], 1))
        self.assertIn("not sent to Housecall Pro", row["response"])


class RefusalTests(BookingBase):
    def test_a_job_can_only_be_booked_once(self):
        res, o, _ = self.book_best()
        with self.assertRaisesRegex(BookingConflict, "already booked"):
            self.book(self.pick(res, o))
        self.assertEqual(len(self.e.q("SELECT 1 FROM bookings")), 1)

    def test_a_slot_taken_by_another_booking_is_refused(self):
        """Two dispatchers look at the same day; the first books, the second's suggestion is stale."""
        with self.e.db.session() as c:
            c.execute("UPDATE technicians SET max_jobs_per_day = 1")        # one job a day: a booking fills the day
            c.execute("DELETE FROM jobs WHERE work_status IN ('scheduled', 'in_progress', 'complete')")
        other = next(u["id"] for u in self.top if u["lat"] is not None and u["id"] != self.job and u["trade_code"] == "PLB")
        res_a, res_b = self.slots(), self.slots(other)
        a = res_a["options"][0]
        b = next(x for x in res_b["options"] if (x["tech_id"], x["date"]) == (a["tech_id"], a["date"]))
        self.book(self.pick(res_a, a))
        with self.assertRaisesRegex(BookingConflict, "no longer available"):
            self.book(self.pick(res_b, b), jid=other)
        self.assertEqual([r["hcp_job_id"] for r in self.e.q("SELECT hcp_job_id FROM bookings")], [self.job])

    def test_a_changed_window_is_refused(self):
        res = self.slots()
        sel = {**self.pick(res, res["options"][0])}
        sel["window_start_min"] += 60
        sel["window_end_min"] += 60
        with self.assertRaisesRegex(BookingConflict, "no longer available"):
            self.book(sel)

    def test_a_changed_neighbour_is_refused(self):
        res = self.slots()
        o = next(x for x in res["options"] if x["before_stop_id"])
        with self.assertRaisesRegex(BookingConflict, "no longer available"):
            self.book({**self.pick(res, o), "before_stop_id": None})
        with self.assertRaisesRegex(BookingConflict, "no longer available"):
            self.book({**self.pick(res, o), "after_stop_id": "job_that_is_not_there"})

    def test_a_wrong_window_length_is_refused(self):
        res = self.slots()
        with self.assertRaisesRegex(BookingConflict, "no longer available"):
            self.book({**self.pick(res, res["options"][0]), "window_minutes": 120})

    def test_a_slot_that_time_has_overtaken_is_refused(self):
        """Suggested at 8:00 for today, confirmed after the day is nearly over."""
        res = self.slots()
        o = next(x for x in res["options"] if x["date"] == TODAY.isoformat())
        late = NOW + timedelta(hours=8)
        with self.assertRaisesRegex(BookingConflict, "no longer available"):
            self.book(self.pick(res, o), now=late)

    def test_a_technician_who_cannot_take_the_job_is_refused(self):
        res = self.slots()
        sel = {**self.pick(res, res["options"][0]), "tech_id": "emp_demo_3"}      # Casey is HVAC only
        with self.assertRaisesRegex(BookingConflict, "no longer available"):
            self.book(sel)

    def test_a_job_hcp_has_scheduled_meanwhile_is_refused(self):
        res = self.slots()
        with self.e.db.session() as c:
            c.execute("UPDATE jobs SET work_status = 'scheduled' WHERE hcp_job_id = ?", (self.job,))
        with self.assertRaisesRegex(BookingConflict, "no longer lists this job as unscheduled"):
            self.book(self.pick(res, res["options"][0]))
        with self.e.db.session() as c:
            c.execute("UPDATE jobs SET work_status = 'unscheduled', active = 0 WHERE hcp_job_id = ?", (self.job,))
        with self.assertRaisesRegex(BookingConflict, "no longer lists this job as unscheduled"):
            self.book(self.pick(res, res["options"][0]))
        self.assertEqual(self.e.q("SELECT 1 FROM bookings"), [])

    def test_unknown_job(self):
        res = self.slots()
        self.assertIsNone(self.book(self.pick(res, res["options"][0]), jid="no-such-job"))

    def test_bad_input_is_rejected_before_anything_is_saved(self):
        res = self.slots()
        good = self.pick(res, res["options"][0])
        far = (TODAY + timedelta(days=14)).isoformat()
        bad = [{**good, "tech_id": None}, {**good, "tech_id": ""}, {**good, "tech_id": 7}, {**good, "date": "soon"},
               {**good, "date": None}, {**good, "date": "2026-09-30"}, {**good, "date": far},
               {**good, "window_start_min": "480"}, {**good, "window_start_min": True}, {**good, "window_start_min": -1},
               {**good, "window_end_min": good["window_start_min"]}, {**good, "window_minutes": 5},
               {**good, "window_minutes": "240"}, {**good, "after_stop_id": 5}, {**good, "before_stop_id": ""},
               {**good, "note": 5}, {**good, "note": "x" * 301}]
        for sel in bad:
            with self.subTest(sel={k: v for k, v in sel.items() if good.get(k) != v}):
                with self.assertRaises(ValueError):
                    self.book(sel)
        self.assertEqual(self.e.q("SELECT 1 FROM bookings"), [])
        self.assertEqual(self.e.q("SELECT 1 FROM schedule_actions"), [])

    def test_a_refused_booking_leaves_no_trace(self):
        res = self.slots()
        sel = {**self.pick(res, res["options"][0]), "window_start_min": 1}
        with self.assertRaises(BookingConflict):
            self.book(sel)
        self.assertEqual((self.e.q("SELECT 1 FROM bookings"), self.e.q("SELECT 1 FROM schedule_actions")), ([], []))


class LockTests(BookingBase):
    def test_the_check_and_the_save_happen_under_one_write_lock(self):
        """While a booking is being checked, nobody else (another booking, a sync) can write."""
        res = self.slots()
        sel = self.pick(res, res["options"][0])
        real, seen = confirm_module.compute_slots, {}

        def spy(*a, **kw):
            other = sqlite3.connect(self.e.cfg.database_path, timeout=0)
            try:
                other.execute("INSERT INTO settings(key, value) VALUES ('probe', '1')")
                seen["locked"] = False
            except sqlite3.OperationalError as err:
                seen["locked"] = "locked" in str(err)
            finally:
                other.close()
            return real(*a, **kw)

        with mock.patch.object(confirm_module, "compute_slots", spy):
            self.book(sel)
        self.assertTrue(seen["locked"])
        self.assertEqual(self.e.q("SELECT 1 FROM settings WHERE key = 'probe'"), [])


class RemoveTests(BookingBase):
    def test_removing_puts_the_job_back_in_the_queue(self):
        _, o, _ = self.book_best()
        with self.e.db.session() as c:
            self.assertTrue(bookings.remove(c, self.job, NOW, self.uid))
        v = self.view(date.fromisoformat(o["date"]))
        self.assertIn(self.job, [u["id"] for u in v["unscheduled"]])
        self.assertEqual(v["bookings"], [])
        self.assertFalse(any(s["id"] == self.job for t in v["technicians"] for s in t["stops"]))
        log = self.e.q("SELECT old_tech, new_tech, response FROM schedule_actions ORDER BY id")
        self.assertEqual((log[1]["old_tech"], log[1]["new_tech"]), (o["tech_id"], None))
        self.assertIn("removed", log[1]["response"])
        self.assertTrue(self.slots()["options"])                     # and it can be booked again

    def test_removing_what_is_not_booked(self):
        with self.e.db.session() as c:
            self.assertFalse(bookings.remove(c, self.job, NOW, self.uid))
            self.assertFalse(bookings.remove(c, "no-such-job", NOW, self.uid))


class LifecycleTests(BookingBase):
    def test_once_hcp_schedules_the_job_the_booking_is_gone(self):
        """HCP is the truth: when a sync shows the job scheduled, our booking goes (and does not come back if the job
        is ever unscheduled again)."""
        _, o, _ = self.book_best()
        j = next(x for x in self.e.hcp.dataset["jobs"] if x["id"] == self.job)
        j.update(work_status="scheduled", schedule={"scheduled_start": o["start_iso"], "scheduled_end": o["end_iso"]},
                 assigned_employees=[{"id": o["tech_id"]}])
        self.e.svc.run(NOW)
        self.assertEqual(self.e.q("SELECT work_status FROM jobs WHERE hcp_job_id = ?", self.job)[0]["work_status"], "scheduled")
        self.assertEqual(self.e.q("SELECT 1 FROM bookings"), [])
        v = self.view(date.fromisoformat(o["date"]))
        stop = next(s for t in v["technicians"] for s in t["stops"] if s["id"] == self.job)
        self.assertFalse(stop["booked"])                              # now it is HCP's stop, not ours
        self.assertEqual(v["bookings"], [])
        j.update(work_status="unscheduled")                           # unscheduled again in HCP
        self.e.svc.run(NOW)
        self.assertIn(self.job, [u["id"] for u in self.view(date.fromisoformat(o["date"]))["unscheduled"]])

    def test_a_sync_that_still_shows_it_unscheduled_keeps_the_booking(self):
        _, o, _ = self.book_best()
        self.e.svc.run(NOW)
        (b,) = self.e.q("SELECT * FROM bookings")
        self.assertEqual((b["hcp_job_id"], b["tech_id"]), (self.job, o["tech_id"]))
        self.assertNotIn(self.job, [u["id"] for u in self.view()["unscheduled"]])

    def test_a_booking_whose_window_has_ended_returns_the_job_to_the_queue(self):
        """Booked here but never entered in HCP: the job must not stay hidden once the promise has passed."""
        _, o, b = self.book_best()
        after = datetime.fromisoformat(b["window_end_iso"].replace("Z", "+00:00")) + timedelta(minutes=1)
        v = self.view(date.fromisoformat(o["date"]), now=after)
        self.assertIn(self.job, [u["id"] for u in v["unscheduled"]])
        self.assertEqual(v["bookings"], [])
        with self.e.db.session() as c:
            self.assertTrue(compute_slots(c, self.job, self.settings, after)["options"])    # searchable again
            self.assertEqual(bookings.drop_stale(c, after), 1)
        self.assertEqual(self.e.q("SELECT 1 FROM bookings"), [])

    def test_an_expired_booking_does_not_block_booking_again(self):
        _, o, b = self.book_best()
        later = datetime.fromisoformat(b["window_end_iso"].replace("Z", "+00:00")) + timedelta(minutes=1)
        res = self.slots(now=later)
        self.assertTrue(res["options"])
        self.book(self.pick(res, res["options"][0]), now=later)
        (row,) = self.e.q("SELECT booked_at FROM bookings")
        self.assertEqual(row["booked_at"], later.strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_a_booking_for_a_job_that_left_the_open_list_is_not_shown(self):
        self.book_best()
        with self.e.db.session() as c:
            c.execute("UPDATE jobs SET active = 0 WHERE hcp_job_id = ?", (self.job,))
        v = self.view()
        self.assertEqual(v["bookings"], [])
        self.assertFalse(any(s["id"] == self.job for t in v["technicians"] for s in t["stops"]))

    def test_the_old_database_gets_the_new_table(self):
        with self.e.db.session() as c:
            c.execute("DROP TABLE bookings")
        from app.db import Database
        Database(self.e.cfg.database_path)                            # opening an old file creates what is missing
        self.assertEqual(self.e.q("SELECT COUNT(*) AS n FROM bookings")[0]["n"], 0)


if __name__ == "__main__":
    unittest.main()
