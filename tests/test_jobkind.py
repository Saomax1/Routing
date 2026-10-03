"""Job types: warranty (Expedited / Normal / Recall, by tag) versus Retail (everything else, ad leads included)."""
import copy
import json
import unittest
from datetime import date, datetime, timezone

from app.domain.jobkind import DEFAULT_JOB_TYPES, TYPE_LABELS, classify_job, count_types, norm_tag, validate_job_types
from app.services.dispatch_view import build_areas, build_dispatch, build_job_detail
from app.services.settings_store import DEFAULT_SETTINGS, deep_merge, get_settings, save_settings, upgrade_stored
from app.hcp.client import HCPClient
from app.services.geocode import MockGeocoder
from app.services.live_check import run_live_check
from app.services.routing import RoadRoutes
from tests.test_api import ApiBase
from tests.test_live import LiveEnv
from tests.test_notes import load_probe
from tests.test_pipeline import Env

NOW = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
SETTINGS = copy.deepcopy(DEFAULT_SETTINGS)


def kind(tags=(), lead="", warranty=None, settings=None):
    return classify_job({"tags": list(tags), "lead_source": lead}, settings or SETTINGS, warranty)


class ClassifyTests(unittest.TestCase):
    def test_the_three_warranty_tags(self):
        for tag, label in (("normal: expedited", "Expedited"), ("normal: Normal", "Normal"), ("normal: recall", "Recall")):
            k = kind([tag])
            self.assertEqual((k["kind"], k["label"], k["tier"], k["ad_lead"]), ("warranty", label, label, False), tag)

    def test_matching_ignores_case_and_spacing(self):
        for tag in ("Normal: Expedited", "NORMAL:EXPEDITED", "  normal :   expedited ", "Normal : Expedited"):
            self.assertEqual(kind([tag])["label"], "Expedited", tag)
        self.assertEqual(norm_tag("Normal :  Recall"), "normal: recall")
        self.assertEqual(kind([{"name": "Normal: Recall"}])["label"], "Recall")                 # tags that arrive as objects

    def test_no_warranty_tag_means_retail(self):
        for tags in ([], ["lead"], ["warranty"], ["ahs"], ["warranty", "ahs"], ["normal"], ["expedited"], ["recall"]):
            k = kind(tags)
            self.assertEqual((k["kind"], k["label"], k["tier"]), ("retail", "Retail", None), tags)      # only the three tags count

    def test_an_ad_lead_is_retail_marked_by_its_tag_or_lead_source(self):
        for k in (kind(["Meta Lead"]), kind(["lead", "META LEAD"]), kind([], lead="Meta Lead")):
            self.assertEqual((k["kind"], k["label"], k["ad_lead"]), ("retail", "Retail", True))
        self.assertEqual(kind(["Meta Lead"])["ad_tag"], "meta lead")
        self.assertFalse(kind(["lead"], lead="Google LSA")["ad_lead"])
        self.assertFalse(kind([], lead="Meta")["ad_lead"])                                       # only the configured tag text

    def test_a_warranty_tag_wins_over_an_ad_tag(self):
        k = kind(["Normal: Normal", "Meta Lead"])
        self.assertEqual((k["kind"], k["label"], k["ad_lead"]), ("warranty", "Normal", False))

    def test_the_most_urgent_tag_wins_if_there_are_several(self):
        self.assertEqual(kind(["Normal: Normal", "Normal: Recall"])["label"], "Recall")
        self.assertEqual(kind(["Normal: Recall", "Normal: Expedited", "Normal: Normal"])["label"], "Expedited")

    def test_warranty_text_does_not_make_a_job_warranty_but_it_is_flagged(self):
        """A warranty call turned into retail still has the old dispatch text. It is retail, and says so."""
        k = kind(["warranty", "ahs"], warranty={"data": {}})
        self.assertEqual((k["kind"], k["label"], k["warranty_text_without_tag"]), ("retail", "Retail", True))
        self.assertFalse(kind(["warranty"])["warranty_text_without_tag"])                        # no text, nothing to flag
        self.assertFalse(kind(["Normal: Normal"], warranty={"data": {}})["warranty_text_without_tag"])   # tagged: it is warranty

    def test_the_tag_names_are_settings(self):
        s = deep_merge(SETTINGS, {"job_types": {"warranty_tags": {"Expedited": ["Rush", "ASAP"]}, "ad_lead_tags": ["Facebook ad"]}})
        self.assertEqual(kind(["asap"], settings=s)["label"], "Expedited")
        self.assertEqual(kind(["Normal: Expedited"], settings=s)["label"], "Retail")             # the old name no longer counts
        self.assertTrue(kind(["facebook AD"], settings=s)["ad_lead"])
        self.assertFalse(kind(["Meta Lead"], settings=s)["ad_lead"])

    def test_empty_lists_turn_a_type_off(self):
        s = deep_merge(SETTINGS, {"job_types": {"warranty_tags": {"Recall": []}}})
        self.assertEqual(kind(["Normal: Recall"], settings=s)["label"], "Retail")
        self.assertEqual(kind(["Normal: Expedited"], settings=s)["label"], "Expedited")

    def test_settings_without_job_types_fall_back_to_the_defaults(self):
        self.assertEqual(classify_job({"tags": ["Normal: Recall"]}, {})["label"], "Recall")
        self.assertEqual(classify_job({"tags": None}, SETTINGS)["label"], "Retail")
        self.assertEqual(classify_job({}, SETTINGS)["label"], "Retail")

    def test_counting_by_type(self):
        kinds = [kind(["normal: normal"]), kind(["normal: normal"]), kind([]), kind(["normal: recall"])]
        self.assertEqual(count_types(kinds), {"Expedited": 0, "Normal": 2, "Recall": 1, "Retail": 1})
        self.assertEqual(list(count_types([])), list(TYPE_LABELS))

    def test_validation(self):
        validate_job_types(DEFAULT_JOB_TYPES)
        for bad in ({"warranty_tags": {"Normal": ["a"], "Recall": [" A "]}}, {"warranty_tags": {"Normal": [""]}},
                    {"warranty_tags": {"Normal": [3]}}, {"warranty_tags": {"Normal": ["x" * 81]}},
                    {"ad_lead_tags": ["normal: normal"], "warranty_tags": {"Normal": ["Normal: Normal"]}}, {"ad_lead_tags": [None]}):
            with self.assertRaises(ValueError, msg=str(bad)):
                validate_job_types(bad)


class SettingsUpgradeTests(unittest.TestCase):
    OLD = {
        "scoring": {"base_by_priority": {"Emergency": 100, "Expedited": 65, "Normal": 25}, "base_direct_lead": 33, "base_other_warranty": 21},
        "scheduling": {"day_penalty_minutes": {"Emergency": 240, "Expedited": 95, "Normal": 16, "Direct": 11}},
        "deadline_rules": {"AHS": {"Expedited": 30, "Normal": 50}, "OTHER_WARRANTY": {"Normal": 52}, "DIRECT": {"Normal": 20}},
    }

    def test_settings_saved_by_an_earlier_version_are_converted_and_keep_the_admins_numbers(self):
        up = upgrade_stored(self.OLD)
        self.assertEqual(up["scoring"]["base_retail"], 33)
        self.assertEqual(up["scoring"]["base_by_priority"], {"Expedited": 65, "Normal": 25})
        for gone in ("base_direct_lead", "base_other_warranty"):
            self.assertNotIn(gone, up["scoring"])
        self.assertEqual(up["scheduling"]["day_penalty_minutes"], {"Expedited": 95, "Normal": 16, "Retail": 11})
        self.assertEqual(up["deadline_rules"]["WARRANTY"], {"Expedited": 30, "Normal": 50, "Recall": 24})      # Recall: the default
        self.assertEqual(up["deadline_rules"]["RETAIL"], {"Retail": 20})
        self.assertEqual(upgrade_stored(up), up)                                                              # converting twice changes nothing

    def test_the_settings_page_can_save_after_an_upgrade(self):
        e = Env()
        try:
            with e.db.session() as c:
                c.execute("INSERT INTO settings(key, value) VALUES ('app', ?)", (json.dumps(self.OLD),))
                s = get_settings(c)
                self.assertEqual((s["scoring"]["base_retail"], s["scoring"]["base_by_priority"]["Recall"]), (33, 40))
                self.assertNotIn("Emergency", s["scoring"]["base_by_priority"])
                save_settings(c, s)                                            # what the Settings page does: send everything back
                self.assertEqual(get_settings(c), s)
        finally:
            e.close()

    def test_an_emptied_rules_table_stays_empty(self):
        self.assertEqual(upgrade_stored({"deadline_rules": {}})["deadline_rules"], {})
        self.assertEqual(upgrade_stored({"deadline_rules": {"WARRANTY": {"Normal": 48}}})["deadline_rules"], {"WARRANTY": {"Normal": 48}})
        self.assertEqual(upgrade_stored(None), {})

    def test_tags_one_type_per_tag_is_checked_across_old_and_new_values(self):
        e = Env()
        try:
            with e.db.session() as c:
                save_settings(c, {"job_types": {"ad_lead_tags": ["promo"]}})
                with self.assertRaises(ValueError):                                       # "promo" would be an ad tag and a Recall tag
                    save_settings(c, {"job_types": {"warranty_tags": {"Recall": ["Promo"]}}})
                self.assertEqual(get_settings(c)["job_types"]["warranty_tags"]["Recall"], ["normal: recall"])
        finally:
            e.close()


class PayloadTests(unittest.TestCase):
    """What the dispatch page is given: the type, and none of the old source / priority wording."""

    @classmethod
    def setUpClass(cls):
        cls.e = Env()
        cls.e.svc.run(NOW)
        with cls.e.db.session() as c:
            cls.settings = get_settings(c)
            cls.view = build_dispatch(c, date(2026, 10, 1), cls.settings, NOW)
            cls.areas = build_areas(c, cls.settings, NOW)
            cls.ids = [r[0] for r in c.execute("SELECT hcp_job_id FROM jobs WHERE active = 1")]
            cls.cards = {i: build_job_detail(c, i, cls.settings, NOW) for i in cls.ids}

    @classmethod
    def tearDownClass(cls):
        cls.e.close()

    def test_every_queue_entry_has_a_type_and_nothing_of_the_old_model(self):
        for u in self.view["unscheduled"]:
            self.assertIn(u["type_label"], TYPE_LABELS)
            self.assertEqual(u["kind"], "retail" if u["type_label"] == "Retail" else "warranty")
            for old in ("source_category", "priority_label", "lead_source"):
                self.assertNotIn(old, u)
        self.assertNotIn("Emergency", json.dumps(self.view["unscheduled"]))
        self.assertEqual(sum(u["ad_lead"] for u in self.view["unscheduled"]), 1)
        self.assertTrue(all(u["kind"] == "retail" for u in self.view["unscheduled"] if u["ad_lead"]))

    def test_scheduled_stops_say_their_type_too(self):
        stops = [s for t in self.view["technicians"] for s in t["stops"]]
        self.assertTrue(stops and all(s["type_label"] in TYPE_LABELS for s in stops))
        self.assertNotIn("source_category", stops[0])

    def test_the_job_card_carries_the_full_classification(self):
        ad = next(c for c in self.cards.values() if c["type"]["ad_lead"])
        self.assertEqual((ad["type"]["kind"], ad["type"]["label"], ad["type"]["ad_tag"]), ("retail", "Retail", "meta lead"))
        rec = [c for c in self.cards.values() if c["type"]["label"] == "Recall"]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["type"]["kind"], "warranty")
        self.assertNotIn("source_category", ad)

    def test_areas_count_the_types(self):
        for a in self.areas["areas"]:
            self.assertEqual(list(a["by_type"]), list(TYPE_LABELS))
            self.assertEqual(sum(a["by_type"].values()), a["count"])
        totals = {t: sum(a["by_type"][t] for a in self.areas["areas"]) for t in TYPE_LABELS}
        self.assertEqual(totals, {"Expedited": 3, "Normal": 4, "Recall": 1, "Retail": 4})

    def test_the_queue_still_carries_no_phone_numbers(self):
        blob = json.dumps(self.view["unscheduled"])
        self.assertNotIn("4805550", blob)


class JobTypeApiTests(ApiBase):
    def test_the_tag_settings_can_be_changed_and_apply_at_once(self):
        a, d = self.admin, self.disp
        before = {u["id"]: u["type_label"] for u in d.get("/api/dispatch").json()["unscheduled"]}
        self.assertEqual(sum(1 for t in before.values() if t == "Recall"), 1)
        try:
            r = a.put("/api/settings", {"settings": {"job_types": {"warranty_tags": {"Recall": ["Normal: Recall", "lead"]}}}})
            self.assertEqual(r.status, 200, r.text)
            after = {u["id"]: u["type_label"] for u in d.get("/api/dispatch").json()["unscheduled"]}
            self.assertEqual(sum(1 for t in after.values() if t == "Recall"), 1 + 3)           # the three jobs tagged "lead" are Recall now
            self.assertEqual(a.put("/api/settings", {"settings": {"job_types": {"warranty_tags": {"Normal": ["Normal: Recall"]}}}}).status, 400)
            self.assertEqual(a.put("/api/settings", {"settings": {"job_types": {"warranty_tags": {"Emergency": ["x"]}}}}).status, 400)
        finally:
            a.put("/api/settings", {"settings": {"job_types": DEFAULT_JOB_TYPES}})
        self.assertEqual({u["id"]: u["type_label"] for u in d.get("/api/dispatch").json()["unscheduled"]}, before)

    def test_a_dispatcher_cannot_change_them(self):
        self.assertEqual(self.disp.put("/api/settings", {"settings": {"job_types": {"ad_lead_tags": ["x"]}}}).status, 403)

    def test_settings_expose_the_job_types(self):
        s = self.disp.get("/api/settings").json()["settings"]
        self.assertEqual(s["job_types"], DEFAULT_JOB_TYPES)
        self.assertEqual(set(s["deadline_rules"]), {"WARRANTY", "RETAIL"})


class LiveTypeCheckTests(unittest.TestCase):
    """Real tag names that differ from the Settings must be easy to spot, without anyone reading code."""

    def setUp(self):
        self.env = LiveEnv(page_size=50)
        self.lines = []

    def tearDown(self):
        self.env.close()

    def check(self):
        self.env.live_sync()
        return run_live_check(self.env.db, self.env.cfg, self.env.client(), MockGeocoder(), RoadRoutes(None), now=NOW, do_sync=False,
                              printer=lambda *a: self.lines.append(" ".join(map(str, a))))

    def retag(self, mapping):
        for j in self.env.fake.dataset["jobs"]:
            j["tags"] = [mapping.get(t, t) for t in j["tags"]]

    def test_the_report_counts_the_types(self):
        rep = self.check()
        jt = rep["job_types"]
        self.assertEqual((jt["Expedited"], jt["Recall"], jt["retail_ad_leads"]), (3, 1, 1))
        self.assertGreater(jt["Normal"], 4)                                   # the unscheduled ones plus the warranty jobs already scheduled
        self.assertGreaterEqual(jt["Retail"], 4)                              # likewise: retail jobs already on the schedule count too
        open_jobs = self.env.q("SELECT COUNT(*) AS n FROM jobs WHERE active = 1 AND work_status != 'complete'")[0]["n"]
        self.assertEqual(sum(jt[t] for t in TYPE_LABELS), open_jobs)          # every open job is exactly one type
        self.assertFalse([c for c in rep["checks"] if "warranty tag" in c["text"]])
        self.assertTrue(any(ln.strip().startswith("open jobs by type: Expedited 3") for ln in self.lines))

    def test_tag_names_that_do_not_match_are_called_out_and_the_names_are_shown_on_screen_only(self):
        self.retag({"Normal: Normal": "Warranty - Normal", "Normal: Expedited": "Warranty - Rush", "Normal: Recall": "Warranty - Callback"})
        rep = self.check()
        self.assertEqual((rep["job_types"]["Expedited"], rep["job_types"]["Normal"], rep["job_types"]["Recall"]), (0, 0, 0))
        warn = [c for c in rep["checks"] if c["level"] == "WARN" and "No job carries a warranty tag" in c["text"]]
        self.assertEqual(len(warn), 1)
        self.assertIn("Admin > Settings > Job types", warn[0]["text"])
        self.assertTrue(any("tags on your jobs" in ln and "Warranty - Rush" in ln for ln in self.lines))        # you can compare them
        self.assertNotIn("Warranty - Rush", json.dumps(rep))                                                   # but they are not in the report
        self.assertEqual(rep["overall"], "WARN")

    def test_no_warranty_tags_and_no_warranty_text_is_only_a_note(self):
        for j in self.env.fake.dataset["jobs"]:
            j["tags"], j["description"] = [], "Service call"
        rep = self.check()
        open_jobs = self.env.q("SELECT COUNT(*) AS n FROM jobs WHERE active = 1 AND work_status != 'complete'")[0]["n"]
        self.assertEqual((rep["job_types"]["Retail"], rep["job_types"]["Expedited"]), (open_jobs, 0))      # everything is retail
        info = [c for c in rep["checks"] if c["level"] == "INFO" and "No job carries one of the warranty tags" in c["text"]]
        self.assertEqual(len(info), 1)
        self.assertFalse([c for c in rep["checks"] if "No job carries a warranty tag," in c["text"]])

    def test_a_few_untagged_warranty_calls_are_noted_not_warned(self):
        victims = [j for j in self.env.fake.dataset["jobs"] if "Normal: Recall" in j["tags"] or "Normal: Expedited" in j["tags"]][:2]
        for j in victims:
            j["tags"] = [t for t in j["tags"] if not t.startswith("Normal:")]
        rep = self.check()
        self.assertEqual(rep["job_types"]["warranty_text_without_tag"], 2)
        info = [c for c in rep["checks"] if c["level"] == "INFO" and "warranty dispatch text but no warranty tag" in c["text"]]
        self.assertEqual(len(info), 1)
        self.assertIn("2 job(s)", info[0]["text"])

    def test_the_probe_shows_how_the_jobs_sort_and_says_when_nothing_matches(self):
        out = []
        rep = load_probe().run_probe(HCPClient(self.env.cfg), limit=3, printer=lambda *a: out.append(" ".join(map(str, a))))
        self.assertEqual((rep["job_types"]["Expedited"], rep["job_types"]["Recall"], rep["job_types"]["Retail (ad lead)"]), (3, 1, 1))
        self.assertNotIn("No job matched a warranty tag", "\n".join(out))
        self.retag({"Normal: Normal": "x1", "Normal: Expedited": "x2", "Normal: Recall": "x3"})
        out.clear()
        rep = load_probe().run_probe(HCPClient(self.env.cfg), limit=3, printer=lambda *a: out.append(" ".join(map(str, a))))
        self.assertFalse({"Expedited", "Normal", "Recall"} & set(rep["job_types"]))
        self.assertIn("No job matched a warranty tag", "\n".join(out))


if __name__ == "__main__":
    unittest.main()
