"""Tests for scoring, duration estimates, travel and the cheapest-insertion slot finder."""
import copy
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.domain.areas import area_of
from app.domain.durations import DEFAULT_DURATIONS, estimate_minutes
from app.domain.scoring import score_job
from app.domain.slots import choose_window, find_best_slots
from app.domain.timeutil import at_local_minutes, ceil_to, parse_iso, to_iso
from app.domain.travel import HaversineTravel, haversine_miles
from app.services.settings_store import DEFAULT_SETTINGS, deep_merge, validate_settings

TZ = ZoneInfo("America/Phoenix")
UTC = timezone.utc
SETTINGS = copy.deepcopy(DEFAULT_SETTINGS)


class GridTravel:
    """Test double: points are (x, y) in minutes; travel = Manhattan distance."""

    def minutes(self, a, b):
        return abs(a[0] - b[0]) + abs(a[1] - b[1])


# ---------------------------------------------------------------------------- scoring

class ScoringTests(unittest.TestCase):
    NOW = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)

    def job(self, hours_ago, category="ahs", desc=""):
        return {"source_category": category, "description_raw": desc,
                "hcp_created_at": to_iso(self.NOW - timedelta(hours=hours_ago))}

    def warranty(self, prio, items=None):
        return {"dispatch_priority": prio, "data": {"items": items or []}}

    def labels(self, res):
        return {b["label"]: b["points"] for b in res["breakdown"]}

    def test_emergency_has_no_deadline_clock(self):
        res = score_job(self.job(3), self.warranty("Emergency", [{"name": "Water Leak", "problem": "secondary damage"}]),
                        SETTINGS, self.NOW)
        lab = self.labels(res)
        self.assertEqual(lab["Priority: Emergency"], 100)
        self.assertEqual(res["deadline_status"], "none")           # the old 4 h AHS Emergency rule is gone
        self.assertIsNone(res["deadline_at"])
        self.assertIsNone(res["deadline_hours_left"])
        self.assertFalse(any(k.startswith("Deadline") for k in lab))
        self.assertEqual(lab["Urgency: secondary damage, leak"] if "Urgency: secondary damage, leak" in lab
                         else lab["Urgency: leak, secondary damage"], 20)
        self.assertEqual(res["priority_label"], "Emergency")

    def test_expedited_close_to_deadline(self):
        res = score_job(self.job(18), self.warranty("Expedited"), SETTINGS, self.NOW)
        self.assertEqual(res["deadline_status"], "critical")       # 6h of a 24h window left = 25%
        self.assertEqual(self.labels(res)["Deadline critical"], 30)
        self.assertAlmostEqual(res["deadline_hours_left"], 6.0, places=1)

    def test_normal_window_is_48_hours(self):
        for category in ("ahs", "other_warranty"):
            for hours_ago, status, left in ((10, "ok", 38), (30, "warning", 18), (46, "critical", 2), (49, "overdue", -1)):
                res = score_job(self.job(hours_ago, category), self.warranty("Normal"), SETTINGS, self.NOW)
                self.assertEqual(res["deadline_status"], status, (category, hours_ago))
                self.assertAlmostEqual(res["deadline_hours_left"], left, places=1)

    def test_exception_excuses_the_deadline(self):
        exc = {"reason": "customer_unavailable", "reason_label": "Customer not available", "note": "back Monday"}
        plain = score_job(self.job(100), self.warranty("Normal"), SETTINGS, self.NOW)
        excused = score_job(self.job(100), self.warranty("Normal"), SETTINGS, self.NOW, exc)
        self.assertEqual(plain["deadline_status"], "overdue")
        self.assertEqual(excused["deadline_status"], "excused")
        self.assertNotIn("Deadline overdue", self.labels(excused))
        self.assertEqual(round(plain["total"] - excused["total"], 1), 40)       # exactly the overdue points
        self.assertLess(excused["deadline_hours_left"], 0)                       # still reported for reference
        self.assertTrue(excused["deadline_at"])
        self.assertEqual(excused["exception"], exc)
        self.assertTrue(any("Customer not available" in b["label"] for b in excused["breakdown"]))

    def test_exception_on_a_job_with_no_deadline_rule_changes_nothing(self):
        exc = {"reason": "other", "reason_label": "Other", "note": "x"}
        res = score_job(self.job(3), self.warranty("Emergency"), SETTINGS, self.NOW, exc)
        self.assertEqual(res["deadline_status"], "none")

    def test_normal_fresh_job_has_no_deadline_points(self):
        res = score_job(self.job(1), self.warranty("Normal"), SETTINGS, self.NOW)
        self.assertEqual(res["deadline_status"], "ok")
        self.assertNotIn("Deadline ok", self.labels(res))
        self.assertLess(res["total"], 25)

    def test_overdue(self):
        res = score_job(self.job(100), self.warranty("Normal"), SETTINGS, self.NOW)   # 48h window
        self.assertEqual(res["deadline_status"], "overdue")
        self.assertLess(res["deadline_hours_left"], 0)
        self.assertEqual(self.labels(res)["Deadline overdue"], 40)

    def test_priority_ordering(self):
        e = score_job(self.job(5), self.warranty("Emergency"), SETTINGS, self.NOW)["total"]
        x = score_job(self.job(5), self.warranty("Expedited"), SETTINGS, self.NOW)["total"]
        n = score_job(self.job(5), self.warranty("Normal"), SETTINGS, self.NOW)["total"]
        self.assertGreater(e, x)
        self.assertGreater(x, n)

    def test_direct_lead(self):
        res = score_job(self.job(0.5, "direct", "AC not blowing cold"), None, SETTINGS, self.NOW)
        self.assertEqual(res["priority_label"], "Direct")
        self.assertEqual(self.labels(res)["Direct lead"], 30)

    def test_urgency_points_are_capped(self):
        text = "leak flood burst sewage backup gas"
        res = score_job(self.job(1, "direct", text), None, SETTINGS, self.NOW)
        urg = [b for b in res["breakdown"] if b["label"].startswith("Urgency")][0]
        self.assertEqual(urg["points"], SETTINGS["scoring"]["urgency_points_cap"])

    def test_age_points_are_capped(self):
        res = score_job(self.job(24 * 60), self.warranty("Normal"), SETTINGS, self.NOW)
        age = [b for b in res["breakdown"] if b["label"].startswith("Waiting")][0]
        self.assertEqual(age["points"], SETTINGS["scoring"]["age_points_cap"])

    def test_missing_created_date(self):
        res = score_job({"source_category": "ahs"}, self.warranty("Expedited"), SETTINGS, self.NOW)
        self.assertEqual(res["deadline_status"], "none")
        self.assertIsNone(res["deadline_at"])
        self.assertEqual(res["total"], 60)

    def test_unknown_priority_is_treated_as_normal(self):
        res = score_job(self.job(1), self.warranty(None), SETTINGS, self.NOW)
        self.assertIn("treated as Normal", res["breakdown"][0]["label"])

    def test_weights_come_from_settings(self):
        s = deep_merge(SETTINGS, {"scoring": {"base_by_priority": {"Expedited": 77}}})
        res = score_job(self.job(1), self.warranty("Expedited"), s, self.NOW)
        self.assertEqual(self.labels(res)["Priority: Expedited"], 77)


# -------------------------------------------------------------------------- durations

class DurationTests(unittest.TestCase):
    ROWS = [{"trade_code": t, "keyword": k, "minutes": m} for t, k, m in DEFAULT_DURATIONS]

    def test_keyword_beats_trade_default(self):
        self.assertEqual(estimate_minutes("PLB", "Water Heater not working", self.ROWS), 120)

    def test_longest_keyword_wins(self):
        rows = self.ROWS + [{"trade_code": "PLB", "keyword": "water heater leak", "minutes": 150}]
        self.assertEqual(estimate_minutes("PLB", "water heater leak at base", rows), 150)

    def test_trade_default_then_any_then_fallback(self):
        self.assertEqual(estimate_minutes("HVAC", "something unusual", self.ROWS), 75)
        self.assertEqual(estimate_minutes("ELEC", "outlet dead", self.ROWS), 60)      # '*' default
        self.assertEqual(estimate_minutes("ELEC", "outlet dead", [], fallback=45), 45)

    def test_keywords_are_whole_words(self):
        self.assertEqual(estimate_minutes("PLB", "leakage report", self.ROWS), 60)    # not "leak"


# ----------------------------------------------------------------------------- travel

class TravelTests(unittest.TestCase):
    def test_same_point_is_zero_and_minimum_applies(self):
        t = HaversineTravel(speed_mph=30, circuity=1.0, min_minutes=3)
        self.assertEqual(t.minutes((33.3, -111.8), (33.3, -111.8)), 0.0)
        self.assertEqual(t.minutes((33.3, -111.8), (33.3, -111.79)), 3.0)             # ~0.58 mi -> 1.2 min -> min 3

    def test_known_distance(self):
        # Chandler center -> Queen Creek center is roughly 14-15 miles straight-line
        d = haversine_miles((33.3062, -111.8413), (33.2487, -111.6343))
        self.assertTrue(12 < d < 16, d)
        t = HaversineTravel(28, 1.3, 3).minutes((33.3062, -111.8413), (33.2487, -111.6343))
        self.assertTrue(25 < t < 50, t)

    def test_symmetric(self):
        t = HaversineTravel()
        a, b = (33.3, -111.8), (33.4, -111.7)
        self.assertAlmostEqual(t.minutes(a, b), t.minutes(b, a))


# ------------------------------------------------------------------------------- slots

def tech(tid="t1", skills=("PLB",), home=(0, 0), start="08:00", end="17:00", days=(0, 1, 2, 3, 4), mx=6, active=True):
    return {"id": tid, "name": tid.upper(), "color": "#123456", "active": active, "trade_skills": list(skills),
            "home_lat": home[0] if home else None, "home_lng": home[1] if home else None,
            "shift_start": start, "shift_end": end, "work_days": list(days), "max_jobs_per_day": mx}


def stop(sid, x, y, start_h, start_m, dur):
    s = start_h * 60 + start_m
    return {"id": sid, "lat": x, "lng": y, "start_min": s, "end_min": s + dur, "label": sid}


class SlotTests(unittest.TestCase):
    # Thursday 2026-10-01, 05:00 local (12:00 UTC) -> same-day earliest start is shift start
    NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    THU, FRI, SAT = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)

    def run_slots(self, techs, schedule, job=None, days=None, duration=60, prio="Normal", now=None, deadline=None, settings=None):
        job = job or {"id": "NEW", "lat": 0, "lng": 20, "trade_code": "PLB"}
        return find_best_slots(job, techs, schedule, GridTravel(), settings or SETTINGS, now or self.NOW,
                               days or [self.THU], duration, TZ, priority_label=prio, deadline_at=deadline)

    def test_empty_day_starts_after_drive_from_home(self):
        res = self.run_slots([tech(home=(0, 0))], {}, job={"id": "NEW", "lat": 0, "lng": 10, "trade_code": "PLB"})
        o = res["options"][0]
        self.assertEqual(o["start_min"], 8 * 60 + 10)        # shift start + 10 min drive
        self.assertEqual(o["added_drive_min"], 10)
        self.assertEqual(o["position"], 1)

    def test_insert_between_two_stops_on_the_way_costs_nothing(self):
        sched = {("t1", self.THU): [stop("A", 0, 10, 9, 0, 60), stop("B", 0, 30, 13, 0, 60)]}
        res = self.run_slots([tech()], sched)               # job at (0,20): exactly between A and B
        o = res["options"][0]
        self.assertEqual(o["added_drive_min"], 0)
        self.assertEqual(o["after_stop_id"], "A")
        self.assertEqual(o["before_stop_id"], "B")
        self.assertEqual(o["start_min"], 10 * 60 + 10)       # A ends 10:00 + 10 min drive
        self.assertEqual([s["id"] for s in o["route_preview"]], ["A", "NEW", "B"])

    def test_gap_too_small_is_rejected(self):
        # A ends 10:00, B starts 10:30: can't fit 60 min + drives. Only "before A" / "after B" remain.
        sched = {("t1", self.THU): [stop("A", 0, 10, 9, 0, 60), stop("B", 0, 30, 10, 30, 60)]}
        res = self.run_slots([tech()], sched)
        for o in res["options"]:
            self.assertNotEqual((o["after_stop_id"], o["before_stop_id"]), ("A", "B"))

    def test_picks_cheapest_position(self):
        # job at (0,30): after B (at 0,30) costs 0 drive; before A costs far more
        sched = {("t1", self.THU): [stop("A", 0, 10, 9, 0, 60), stop("B", 0, 30, 11, 0, 60)]}
        res = self.run_slots([tech()], sched, job={"id": "NEW", "lat": 0, "lng": 30, "trade_code": "PLB"})
        self.assertEqual(res["options"][0]["after_stop_id"], "B")
        self.assertEqual(res["options"][0]["added_drive_min"], 0)

    def test_skill_mismatch_is_reported_not_silent(self):
        res = self.run_slots([tech(skills=("HVAC",))], {})
        self.assertEqual(res["options"], [])
        self.assertIn("No PLB skill", res["ineligible"][0]["reason"])

    def test_tech_without_skills_or_inactive_is_reported(self):
        res = self.run_slots([tech("a", skills=()), tech("b", active=False)], {})
        reasons = {i["tech_id"]: i["reason"] for i in res["ineligible"]}
        self.assertIn("skills", reasons["a"])
        self.assertEqual(reasons["b"], "Inactive")

    def test_max_jobs_per_day(self):
        sched = {("t1", self.THU): [stop("A", 0, 10, 9, 0, 30), stop("B", 0, 12, 11, 0, 30)]}
        res = self.run_slots([tech(mx=2)], sched)
        self.assertEqual(res["options"], [])
        self.assertIn("Fully booked", res["ineligible"][0]["reason"])

    def test_non_working_day_excluded(self):
        res = self.run_slots([tech()], {}, days=[self.SAT])
        self.assertEqual(res["options"], [])
        self.assertIn("Not working", res["ineligible"][0]["reason"])

    def test_job_too_long_for_shift(self):
        res = self.run_slots([tech(start="08:00", end="09:00")], {}, duration=90)
        self.assertEqual(res["options"], [])

    def test_same_day_lead_time_respected(self):
        now = datetime(2026, 10, 1, 17, 7, tzinfo=UTC)       # 10:07 local
        res = self.run_slots([tech(home=(0, 20))], {}, now=now)
        self.assertGreaterEqual(res["options"][0]["start_min"], 10 * 60 + 37)  # >= 10:07 + 30 min lead
        self.assertEqual(res["options"][0]["start_min"] % 5, 0)

    def test_emergency_prefers_today_normal_prefers_cheaper_tomorrow(self):
        # Today the tech has a short job far away at (0,100) 08:30-09:00. The new job at (0,20) can only
        # go AFTER it (before it, the tech could not reach A by 08:30), costing 80 min of added driving.
        # Tomorrow's empty day costs only 20 min (home -> job). Emergency (240 min/day delay penalty)
        # should still take today; Normal (15 min/day) should take the cheaper tomorrow.
        sched = {("t1", self.THU): [stop("A", 0, 100, 8, 30, 30)]}
        t = tech(home=(0, 0))
        em = self.run_slots([t], sched, days=[self.THU, self.FRI], prio="Emergency")
        no = self.run_slots([t], sched, days=[self.THU, self.FRI], prio="Normal")
        self.assertEqual(em["options"][0]["date"], self.THU.isoformat())
        self.assertEqual(no["options"][0]["date"], self.FRI.isoformat())

    def test_one_best_option_per_tech_day_and_top_n(self):
        techs = [tech(f"t{i}", home=(0, i)) for i in range(8)]
        res = self.run_slots(techs, {}, days=[self.THU, self.FRI])
        self.assertEqual(len(res["options"]), SETTINGS["scheduling"]["top_n"])
        keys = [(o["tech_id"], o["date"]) for o in res["options"]]
        self.assertEqual(len(keys), len(set(keys)))
        costs = [o["cost"] for o in res["options"]]
        self.assertEqual(costs, sorted(costs))

    def test_deadline_miss_is_flagged_and_penalised(self):
        deadline = at_local_minutes(self.THU, 9 * 60, TZ)             # must be done by 09:00 today
        res = self.run_slots([tech(home=(0, 0))], {}, days=[self.THU, self.FRI], deadline=deadline,
                             job={"id": "NEW", "lat": 0, "lng": 50, "trade_code": "PLB"})   # 50 min drive -> ends 09:50
        self.assertTrue(all(o["misses_deadline"] for o in res["options"]))

    def test_already_passed_deadline_is_not_flagged_on_every_option(self):
        deadline = at_local_minutes(self.THU, 1 * 60, TZ)              # 01:00 today: already past at 05:00 local
        res = self.run_slots([tech(home=(0, 0))], {}, days=[self.THU, self.FRI], deadline=deadline)
        self.assertTrue(res["options"])
        self.assertFalse(any(o["misses_deadline"] for o in res["options"]))
        self.assertTrue(any("past its deadline" in n for n in res["notes"]))
        self.assertEqual(res["options"][0]["date"], self.THU.isoformat())   # still prefers the soonest slot

    def test_soonest_mode_ranks_by_start_time_not_drive_cost(self):
        # t1 sits right at the job but is busy until 12:00; t2 is free but 50 min away (earliest 08:50).
        sched = {("t1", self.THU): [stop("A", 0, 20, 8, 0, 240)]}
        techs = [tech("t1", home=(0, 20)), tech("t2", home=(0, 70))]
        cheapest = self.run_slots(techs, sched)
        soonest = find_best_slots({"id": "NEW", "lat": 0, "lng": 20, "trade_code": "PLB"}, techs, sched, GridTravel(),
                                  SETTINGS, self.NOW, [self.THU], 60, TZ, soonest=True)
        self.assertEqual(cheapest["options"][0]["tech_id"], "t1")          # zero extra driving wins by default
        self.assertEqual(soonest["options"][0]["tech_id"], "t2")           # but t2 can be there first
        self.assertEqual(soonest["options"][0]["start_min"], 8 * 60 + 50)
        self.assertEqual([o["tech_id"] for o in soonest["options"]], ["t2", "t1"])

    def test_soonest_mode_prefers_an_earlier_day_over_a_closer_gap(self):
        res = find_best_slots({"id": "NEW", "lat": 0, "lng": 20, "trade_code": "PLB"}, [tech(home=(0, 0))], {},
                              GridTravel(), SETTINGS, self.NOW, [self.THU, self.FRI], 60, TZ, soonest=True)
        self.assertEqual([o["date"] for o in res["options"]], [self.THU.isoformat(), self.FRI.isoformat()])

    def test_limit_overrides_top_n(self):
        techs = [tech(f"t{i}", home=(0, i)) for i in range(8)]
        res = find_best_slots({"id": "NEW", "lat": 0, "lng": 20, "trade_code": "PLB"}, techs, {}, GridTravel(),
                              SETTINGS, self.NOW, [self.THU, self.FRI], 60, TZ, soonest=True, limit=100)
        self.assertEqual(len(res["options"]), 16)                          # one per tech per day, not capped at 5

    def test_job_without_location(self):
        res = self.run_slots([tech()], {}, job={"id": "NEW", "lat": None, "lng": None, "trade_code": "PLB"})
        self.assertEqual(res["options"], [])
        self.assertTrue(res["notes"])

    def test_tech_without_home_base_still_works(self):
        res = self.run_slots([tech(home=None)], {})
        self.assertEqual(res["options"][0]["start_min"], 8 * 60)      # no drive-in assumed
        self.assertIsNone(res["options"][0]["home"])

    def test_unknown_trade_matches_any_skilled_tech(self):
        res = self.run_slots([tech(skills=("HVAC",))], {}, job={"id": "NEW", "lat": 0, "lng": 5, "trade_code": ""})
        self.assertEqual(len(res["options"]), 1)

    def test_existing_stops_never_move(self):
        sched = {("t1", self.THU): [stop("A", 0, 10, 9, 0, 60), stop("B", 0, 30, 13, 0, 60)]}
        before = copy.deepcopy(sched)
        self.run_slots([tech()], sched)
        self.assertEqual(sched, before)


class ChooseWindowTests(unittest.TestCase):
    H = 60

    def test_window_starts_on_the_hour_at_or_before_the_arrival(self):
        self.assertEqual(choose_window(8 * 60 + 20, 480, 1020, 240, 60), (480, 720))            # arrive 8:20 -> 8-12
        self.assertEqual(choose_window(10 * 60 + 20, 480, 1020, 240, 60), (600, 840))           # arrive 10:20 -> 10-2
        self.assertEqual(choose_window(8 * 60 + 50, 480, 1020, 240, 30), (510, 750))            # 30-minute grid -> 8:30-12:30

    def test_late_arrivals_keep_the_whole_window_inside_the_shift(self):
        self.assertEqual(choose_window(15 * 60 + 10, 480, 1020, 240, 60), (780, 1020))          # 1-5 PM, not 3-7 PM
        self.assertEqual(choose_window(8 * 60, 480, 540, 240, 60), (480, 540))                  # shift shorter than a window

    def test_today_a_window_never_starts_in_the_past(self):
        self.assertEqual(choose_window(14 * 60 + 40, 480, 1020, 240, 60, min_start=14 * 60), (840, 1020))   # 2-5 PM

    def test_a_window_shorter_than_the_grid_step_still_contains_the_arrival(self):
        ws, we = choose_window(8 * 60 + 50, 480, 1020, 30, 60)
        self.assertTrue(ws <= 530 <= we)
        self.assertEqual(we - ws, 30)

    def test_the_arrival_is_always_inside_its_window(self):
        for window in (15, 30, 60, 120, 240, 480, 720):
            for grid in (5, 15, 30, 60, 120):
                for min_start in (None, 600, 840):
                    for eta in range(max(480, min_start or 0), 1000, 7):
                        ws, we = choose_window(eta, 480, 1020, window, grid, min_start)
                        ctx = (window, grid, min_start, eta, ws, we)
                        self.assertTrue(ws <= eta <= we, ctx)
                        self.assertTrue(480 <= ws and we <= 1020 and we - ws <= window, ctx)
                        if min_start is not None:
                            self.assertGreaterEqual(ws, min_start, ctx)


def wstop(sid, x, y, start_h, start_m, dur, window=240, state=None):
    """An existing stop with an arrival window starting at its scheduled time."""
    s = stop(sid, x, y, start_h, start_m, dur)
    s["win_start_min"], s["win_end_min"] = s["start_min"], s["start_min"] + window
    if state:
        s["state"] = state
    return s


class WindowSlotTests(unittest.TestCase):
    THU = date(2026, 10, 1)
    NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)               # Thu 05:00 local: the tech is free from shift start

    def find(self, techs, sched, job=(0, 10), now=None, duration=60, settings=None, **kw):
        job = {"id": "NEW", "lat": job[0], "lng": job[1], "trade_code": "PLB"}
        return find_best_slots(job, techs, sched, GridTravel(), settings or SETTINGS, now or self.NOW, [self.THU],
                               duration, TZ, **kw)

    def local(self, hh, mm):                                      # a "now" at hh:mm Phoenix time on the test Thursday
        return datetime(2026, 10, 1, hh + 7, mm, tzinfo=UTC)

    def tuned(self, **sched):
        st = copy.deepcopy(SETTINGS)
        st["scheduling"].update(sched)
        return st

    # ---- the window offered
    def test_the_standard_window_is_four_hours(self):
        o = self.find([tech(home=(0, 0))], {})["options"][0]
        self.assertEqual((o["start_min"], o["window_start_min"], o["window_end_min"]), (490, 480, 720))   # arrive ~8:10 in 8-12
        self.assertEqual(o["window_minutes"], 240)
        self.assertEqual(o["start_iso"], "2026-10-01T15:10:00Z")
        self.assertEqual((o["window_start_iso"], o["window_end_iso"]), ("2026-10-01T15:00:00Z", "2026-10-01T19:00:00Z"))

    def test_the_window_length_can_be_changed_per_search_and_in_settings(self):
        res = self.find([tech(home=(0, 0))], {}, window_min=120)
        self.assertEqual((res["options"][0]["window_start_min"], res["options"][0]["window_end_min"]), (480, 600))
        self.assertEqual(res["window_minutes"], 120)
        res = self.find([tech(home=(0, 0))], {}, settings=self.tuned(window_minutes=180))
        self.assertEqual(res["options"][0]["window_minutes"], 180)
        res = self.find([tech(home=(0, 0))], {}, settings=self.tuned(window_minutes=180), window_min=300)
        self.assertEqual(res["options"][0]["window_minutes"], 300)              # the per-search value wins

    def test_a_late_arrival_gets_a_window_that_still_ends_with_the_shift(self):
        o = self.find([tech(home=(0, 0))], {}, now=self.local(14, 40))["options"][0]    # free from ~15:10, today
        self.assertEqual(o["start_min"], 15 * 60 + 20)
        self.assertEqual((o["window_start_min"], o["window_end_min"]), (14 * 60, 17 * 60))   # not in the past, ends at 5 PM

    # ---- overlapping windows and "same window" suggestions
    def test_a_nearby_job_is_offered_the_same_window(self):
        sched = {("t1", self.THU): [wstop("A", 0, 20, 8, 0, 60)]}                # A: 8-12, 10 min from the new job
        o = self.find([tech(home=(0, 0))], sched, job=(0, 30))["options"][0]
        self.assertEqual(o["after_stop_id"], "A")
        self.assertEqual(o["start_min"], 9 * 60 + 30)                            # arrive after A: 8:20-9:20, +10 drive
        self.assertEqual((o["window_start_min"], o["window_end_min"]), (480, 720))   # same 8-12 as A, not 9-1
        self.assertEqual(o["stacked_with"], [{"id": "A", "label": "A", "drive_min": 10.0, "same_window": True,
                                              "window_start_min": 480, "window_end_min": 720}])

    def test_a_farther_job_gets_its_own_overlapping_window(self):
        sched = {("t1", self.THU): [wstop("A", 0, 20, 8, 0, 60)]}                # A is 40 min away: not "close"
        o = self.find([tech(home=(0, 0))], sched, job=(0, 60))["options"][0]
        self.assertEqual(o["start_min"], 10 * 60)
        self.assertEqual((o["window_start_min"], o["window_end_min"]), (600, 840))   # A 8-12, new 10-2: windows overlap
        self.assertEqual(o["stacked_with"], [])

    def test_a_nearby_job_with_a_partly_overlapping_window_is_reported_as_overlapping_not_the_same(self):
        # A: 8:00, window until 9:45. The new job is 27 min away (stacking distance raised to 30) and is reached at
        # 9:50, after A's window: it cannot share it, but its own 9-1 window overlaps A's.
        sched = {("t1", self.THU): [wstop("A", 0, 20, 8, 0, 60, window=105)]}
        o = self.find([tech(home=(0, 0))], sched, job=(0, 47), settings=self.tuned(stack_within_minutes=30))["options"][0]
        self.assertEqual((o["start_min"], o["window_start_min"]), (590, 540))
        (x,) = o["stacked_with"]
        self.assertEqual((x["id"], x["same_window"], x["window_start_min"], x["window_end_min"]), ("A", False, 480, 585))

    def test_the_stacking_distance_is_adjustable(self):
        sched = {("t1", self.THU): [wstop("A", 0, 20, 8, 0, 60)]}
        wide = self.find([tech(home=(0, 0))], sched, job=(0, 60), settings=self.tuned(stack_within_minutes=45))["options"][0]
        self.assertEqual((wide["window_start_min"], wide["stacked_with"][0]["id"]), (480, "A"))
        off = self.find([tech(home=(0, 0))], sched, job=(0, 30), settings=self.tuned(stack_within_minutes=0))["options"][0]
        self.assertEqual((off["window_start_min"], off["stacked_with"]), (540, []))   # 10 min away but stacking is off

    def test_a_stop_is_only_joined_if_its_window_holds_the_arrival_and_touching_is_not_overlap(self):
        sched = {("t1", self.THU): [wstop("A", 0, 20, 8, 0, 60, window=60)]}     # A: 8-9 only, but we arrive 9:30
        o = self.find([tech(home=(0, 0))], sched, job=(0, 30))["options"][0]
        self.assertEqual((o["window_start_min"], o["stacked_with"]), (540, []))

    # ---- windows give slack that fixed times did not
    def test_big_windows_make_room_where_fixed_times_had_none(self):
        # A at 9:00 (0,10), B at 10:30 (0,30), a 60-minute job at (0,20) needs 11:20 to reach B
        fixed = {("t1", self.THU): [stop("A", 0, 10, 9, 0, 60), stop("B", 0, 30, 10, 30, 60)]}
        wide = {("t1", self.THU): [wstop("A", 0, 10, 9, 0, 60), wstop("B", 0, 30, 10, 30, 60)]}
        between = lambda res: any((o["after_stop_id"], o["before_stop_id"]) == ("A", "B") for o in res["options"])
        self.assertFalse(between(self.find([tech()], fixed, job=(0, 20))))
        res = self.find([tech()], wide, job=(0, 20))
        self.assertTrue(between(res))
        o = next(o for o in res["options"] if (o["after_stop_id"], o["before_stop_id"]) == ("A", "B"))
        self.assertEqual([(p["id"], p["start_min"]) for p in o["route_preview"]],
                         [("A", 540), ("NEW", 610), ("B", 680)])                # B is reached at 11:20, inside its window

    def test_an_existing_window_is_never_broken(self):
        for window, ok in ((60, True), (30, False)):                            # B opens 10:30; we reach it at 11:20
            sched = {("t1", self.THU): [wstop("A", 0, 10, 9, 0, 60), wstop("B", 0, 30, 10, 30, 60, window=window)]}
            res = self.find([tech()], sched, job=(0, 20))
            got = any((o["after_stop_id"], o["before_stop_id"]) == ("A", "B") for o in res["options"])
            self.assertEqual(got, window == 60 and ok, window)

    def test_stops_are_ordered_by_planned_arrival_not_by_window_start(self):
        """B was booked into a wide 1-5 PM window but arrives at 3:10, after A (1:30-2:30). B opens earlier than A, yet
        the route is still A then B: a job dropped between them goes between them."""
        a = wstop("A", 0, 10, 13, 30, 60, window=60)
        b = {"id": "B", "lat": 0, "lng": 30, "start_min": 910, "end_min": 970, "label": "B",
             "win_start_min": 780, "win_end_min": 1020}
        res = self.find([tech(home=(0, 0))], {("t1", self.THU): [b, a]}, job=(0, 20))
        o = res["options"][0]
        self.assertEqual((o["after_stop_id"], o["before_stop_id"], o["added_drive_min"]), ("A", "B", 0))
        self.assertEqual([p["id"] for p in o["route_preview"]], ["A", "NEW", "B"])

    def test_a_stop_that_is_already_late_is_not_made_later(self):
        # A (0,100) is due at 8:30 but the tech is 100 min away: already late. After A is fine, before A is not.
        sched = {("t1", self.THU): [wstop("A", 0, 100, 8, 30, 30, window=0)]}
        res = self.find([tech(home=(0, 0))], sched, job=(0, 20))
        self.assertEqual([(o["after_stop_id"], o["before_stop_id"]) for o in res["options"]], [("A", None)])

    def test_a_stop_whose_window_closed_before_now_does_not_block_the_day(self):
        sched = {("t1", self.THU): [wstop("L", 0, 10, 8, 0, 60, window=60)]}    # due 8-9, tech running late at 10:00
        o = self.find([tech(home=(0, 0))], sched, job=(0, 20), now=self.local(10, 0))["options"][0]
        self.assertEqual(o["after_stop_id"], "L")
        self.assertEqual(o["start_min"], 11 * 60 + 50)                          # L reached 10:40, done 11:40, +10 drive

    # ---- completed and in-progress work
    def test_a_completed_job_is_the_starting_point_and_counts_toward_the_daily_max(self):
        done = stop("C", 0, 50, 8, 0, 60)
        done["state"] = "complete"
        sched = {("t1", self.THU): [done]}
        noon = self.local(12, 0)
        o = self.find([tech(home=(0, 0))], sched, job=(0, 60), now=noon)["options"][0]
        self.assertEqual((o["drive_in_min"], o["origin"]["kind"]), (10, "complete"))         # from C, not from home
        self.assertEqual(o["start_min"], 12 * 60 + 40)
        self.assertEqual((o["stops_in_day"], o["position"]), (1, 2))          # the second stop of the day: C came first
        home_start = self.find([tech(home=(0, 0))], {}, job=(0, 60), now=noon)["options"][0]
        self.assertEqual((home_start["drive_in_min"], home_start["origin"]["kind"]), (60, "home"))
        full = self.find([tech(home=(0, 0), mx=1)], sched, job=(0, 60), now=noon)
        self.assertEqual(full["options"], [])
        self.assertIn("Fully booked", full["ineligible"][0]["reason"])

    def test_a_job_in_progress_keeps_the_technician_busy_until_it_ends(self):
        busy = stop("P", 0, 20, 11, 0, 120)                                     # 11:00-13:00
        busy["state"] = "in_progress"
        o = self.find([tech(home=(0, 0))], {("t1", self.THU): [busy]}, job=(0, 30), now=self.local(12, 0))["options"][0]
        self.assertEqual(o["start_min"], 13 * 60 + 10)                          # free at 13:00 + 10 min drive
        self.assertEqual(o["origin"]["kind"], "in_progress")

    def test_completed_jobs_are_not_planned_again_or_shown_in_the_preview(self):
        done = stop("C", 0, 5, 8, 0, 60)
        done["state"] = "complete"
        open_ = wstop("A", 0, 30, 13, 0, 60)
        o = self.find([tech(home=(0, 0))], {("t1", self.THU): [done, open_]}, job=(0, 20), now=self.local(10, 0))["options"][0]
        self.assertNotIn("C", [p["id"] for p in o["route_preview"]])
        self.assertEqual(o["stops_in_day"], 2)

    def test_the_schedule_passed_in_is_never_modified(self):
        sched = {("t1", self.THU): [wstop("A", 0, 20, 8, 0, 60), wstop("B", 0, 40, 11, 0, 60)]}
        before = copy.deepcopy(sched)
        self.find([tech()], sched, job=(0, 30))
        self.assertEqual(sched, before)


class AreaTests(unittest.TestCase):
    def test_city_grouping_is_case_and_spacing_insensitive(self):
        a = area_of({"city": "  CHANDLER ", "zip": "85226"})
        self.assertEqual(a, area_of({"city": "chandler", "zip": "85225"}))
        self.assertEqual(a, ("city:chandler", "Chandler"))
        self.assertEqual(area_of({"city": "McKinney"})[1], "McKinney")           # mixed case is left alone

    def test_zip_grouping_and_fallbacks(self):
        self.assertEqual(area_of({"city": "Mesa", "zip": "85201-1234"}, "zip"), ("zip:85201", "ZIP 85201"))
        self.assertEqual(area_of({"city": "Mesa", "zip": ""}, "zip"), ("city:mesa", "Mesa"))      # no zip: use city
        self.assertEqual(area_of({"city": "", "zip": "85201"}, "city"), ("zip:85201", "ZIP 85201"))  # no city: use zip
        self.assertEqual(area_of({"city": "", "zip": "abc"}), ("none", "No address"))
        self.assertEqual(area_of({}), ("none", "No address"))


class MiscTests(unittest.TestCase):
    def test_iso_roundtrip_and_ceil(self):
        dt = parse_iso("2026-10-01T15:30:00Z")
        self.assertEqual(to_iso(dt), "2026-10-01T15:30:00Z")
        self.assertEqual(parse_iso("2026-10-01T08:30:00-07:00"), dt)
        self.assertIsNone(parse_iso("garbage"))
        self.assertEqual(ceil_to(481, 5), 485)
        self.assertEqual(ceil_to(480, 5), 480)

    def test_default_deadline_rules(self):
        rules = DEFAULT_SETTINGS["deadline_rules"]
        self.assertNotIn("Emergency", rules["AHS"])                 # AHS Emergency has no deadline clock
        self.assertEqual(rules["AHS"]["Normal"], 48)
        self.assertEqual(rules["OTHER_WARRANTY"]["Normal"], 48)
        self.assertEqual(DEFAULT_SETTINGS["areas"]["group_by"], "city")
        sched = DEFAULT_SETTINGS["scheduling"]
        self.assertEqual((sched["window_minutes"], sched["window_step_minutes"], sched["stack_within_minutes"]), (240, 60, 20))

    def test_settings_validation(self):
        validate_settings({"scoring": {"base_direct_lead": 40}})
        validate_settings({"areas": {"group_by": "zip"}})
        validate_settings({"scheduling": {"window_minutes": 180, "window_step_minutes": 30, "stack_within_minutes": 0}})
        for bad in ({"scoring": {"base_direct_lead": "x"}}, {"nope": 1}, {"timezone": "Mars/Base"},
                    {"deadline_rules": {"AHS": {"Normal": -1}}}, {"map": {"tile_url": "http://insecure/{z}/{x}/{y}"}},
                    {"deadline_rules": {"AHS": 5}}, {"areas": {"group_by": "county"}}, {"areas": {"nope": 1}},
                    {"scheduling": {"window_minutes": 5}}, {"scheduling": {"window_minutes": 721}},
                    {"scheduling": {"window_step_minutes": 0}}, {"scheduling": {"stack_within_minutes": -1}},
                    {"scheduling": {"stack_within_minutes": 500}}):
            with self.assertRaises(ValueError, msg=str(bad)):
                validate_settings(bad)


if __name__ == "__main__":
    unittest.main()
