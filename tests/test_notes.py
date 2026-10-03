"""Warranty dispatch text kept in a job's PRIVATE NOTES: it is read; every other note never reaches the database."""
import copy
import importlib.util
import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from app.domain.warranty_parser import looks_like_warranty, parse_warranty_job
from app.hcp.client import HCPClient
from app.hcp.fixtures import build_ahs_description
from app.hcp.normalize import normalize_job, note_texts, warranty_notes
from app.services.dispatch_view import build_dispatch, build_job_detail
from app.services.live_check import run_live_check
from app.services.geocode import MockGeocoder
from app.services.routing import RoadRoutes
from app.services.settings_store import get_settings
from tests.test_live import LiveEnv

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
SECRETS = ("Gate code 4521", "dog in the back yard", "owes us $300")
PRIVATE_REMARK = "Gate code 4521. Beware: dog in the back yard. Customer owes us $300 from last time."


def ahs(priority="Emergency", trade="PLB"):
    return build_ahs_description(
        dispatch="92000123", trade=trade, priority=priority, name="PAT EXAMPLE", phone="4805550123",
        street="1 TEST ST", city="CHANDLER", zip_code="85225", items=[{"name": "Water Heater", "problem": "Leaking"}],
        total=100, svc_req="22000123")


class NoteShapeTests(unittest.TestCase):
    JOB = {"id": "job_x", "work_status": "unscheduled"}

    def job(self, **fields):
        return normalize_job({**self.JOB, **fields})

    def test_the_warranty_text_in_private_notes_is_read_and_parses(self):
        n = self.job(notes=[{"id": "n1", "content": PRIVATE_REMARK}, {"id": "n2", "content": ahs("Emergency", "PLB")}])
        self.assertTrue(looks_like_warranty(n["description_raw"]))
        w = parse_warranty_job(n["description_raw"])
        self.assertEqual((w.dispatch_priority, w.trade_code, w.dispatch_number), ("Emergency", "PLB", "92000123"))

    def test_other_private_notes_are_dropped_before_anything_can_store_them(self):
        n = self.job(notes=[{"content": PRIVATE_REMARK}, {"content": ahs()}, {"content": "Call before arriving"}])
        for secret in SECRETS + ("Call before arriving",):
            self.assertNotIn(secret, json.dumps(n))                 # not in any field of the normalized job
        self.assertEqual(self.job(notes=[{"content": PRIVATE_REMARK}])["description_raw"], "")
        self.assertEqual(self.job(notes=PRIVATE_REMARK)["description_raw"], "")

    def test_description_and_a_warranty_note_are_combined_description_first(self):
        n = self.job(description="Water heater leaking", notes=[{"content": PRIVATE_REMARK}, {"content": ahs()}])
        self.assertTrue(n["description_raw"].startswith("Water heater leaking\n\n"))
        self.assertIn("Dispatch Priority", n["description_raw"])
        for secret in SECRETS:
            self.assertNotIn(secret, n["description_raw"])

    def test_a_dispatch_that_is_in_both_places_is_not_doubled(self):
        text = ahs()
        n = self.job(description=text, notes=[{"content": text}])
        self.assertEqual(n["description_raw"], text)

    def test_every_shape_a_notes_field_can_take(self):
        text = ahs()
        shapes = [{"notes": [{"content": text}]}, {"notes": [{"text": text}]}, {"notes": [{"note": text}]}, {"notes": [{"body": text}]},
                  {"notes": [text]}, {"notes": text}, {"private_notes": [{"content": text}]}, {"internal_notes": text},
                  {"job_notes": [{"id": 1, "content": text, "author": "someone"}]}]
        for fields in shapes:
            with self.subTest(fields=list(fields)):
                self.assertEqual(self.job(**fields)["description_raw"], text.strip())      # notes are trimmed

    def test_odd_values_are_ignored_not_fatal(self):
        for notes in (None, [], [None, 5, {"id": 1}, {"content": None}, {"content": "   "}, [1, 2]], {"content": 7}, 12, True):
            with self.subTest(notes=notes):
                self.assertEqual(self.job(notes=notes)["description_raw"], "")
        self.assertEqual(note_texts({"notes": [{"content": " a "}, "b"]}), ["a", "b"])
        self.assertEqual(warranty_notes({"notes": ["a", ahs()]}), [ahs().strip()])

    def test_jobs_without_notes_behave_as_before(self):
        self.assertEqual(self.job(description=ahs())["description_raw"], ahs())
        self.assertEqual(self.job()["description_raw"], "")


def move_warranty_text_to_private_notes(dataset: dict) -> dict:
    """What a company that pastes the warranty dispatch into PRIVATE NOTES looks like: a bare description, a private
    remark nobody should read, and the dispatch text in a note."""
    ds = copy.deepcopy(dataset)
    for j in ds["jobs"]:
        text, notes = j.get("description") or "", [{"id": "n1", "content": PRIVATE_REMARK}]
        if looks_like_warranty(text):
            notes.append({"id": "n2", "content": text})
            j["description"] = "Service call"
        j["notes"] = notes
    return ds


def load_probe():
    spec = importlib.util.spec_from_file_location("probe_under_test", ROOT / "scripts" / "phase0_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PrivateNotesEndToEndTests(unittest.TestCase):
    """Over real HTTP against the stand-in Housecall Pro: the same jobs, with the dispatch text in private notes."""

    def setUp(self):
        self.base = LiveEnv()
        self.notes = LiveEnv()
        self.notes.fake.dataset = move_warranty_text_to_private_notes(self.notes.fake.dataset)

    def tearDown(self):
        self.base.close()
        self.notes.close()

    def parsed(self, env):
        return {r["hcp_job_id"]: (r["dispatch_priority"], r["trade_code"], r["dispatch_number"])
                for r in env.q("SELECT hcp_job_id, dispatch_priority, trade_code, dispatch_number FROM warranty_details")}

    def test_warranty_jobs_come_out_the_same_whether_the_text_is_in_the_description_or_the_notes(self):
        self.assertEqual(self.base.live_sync()["status"], "ok")
        self.assertEqual(self.notes.live_sync()["status"], "ok")
        before, after = self.parsed(self.base), self.parsed(self.notes)
        self.assertGreater(len(before), 20)
        self.assertEqual(after, before)
        types = {}
        for name, env in (("base", self.base), ("notes", self.notes)):
            with env.db.session() as c:
                u = build_dispatch(c, date(2026, 10, 1), get_settings(c), NOW)["unscheduled"]
            types[name] = sorted((x["id"], x["type_label"]) for x in u)
        self.assertEqual(types["notes"], types["base"])                  # the tags are untouched, so the types are too
        self.assertIn("Expedited", {t for _, t in types["notes"]})

    def test_private_remarks_never_reach_the_database_or_the_job_card(self):
        self.notes.live_sync()
        with self.notes.db.session() as c:
            dump = "\n".join(c.iterdump())
            for secret in SECRETS:
                self.assertNotIn(secret, dump)
            settings = get_settings(c)
            ids = [r[0] for r in c.execute("SELECT hcp_job_id FROM jobs")]
            cards = json.dumps([build_job_detail(c, i, settings, NOW) for i in ids])
        for secret in SECRETS:
            self.assertNotIn(secret, cards)
        self.assertIn("Dispatch Priority", cards)                    # while the dispatch text itself is on the card

    def test_the_notes_are_read_with_the_same_reads_as_before(self):
        self.notes.live_sync()
        self.assertEqual({(r["method"], r["path"]) for r in self.notes.fake.requests}, {("GET", "/employees"), ("GET", "/jobs")})

    def test_the_probe_says_where_the_warranty_text_is_and_prints_no_private_remark(self):
        out = []
        report = load_probe().run_probe(HCPClient(self.notes.cfg), limit=5, printer=lambda *a: out.append(" ".join(map(str, a))))
        pn = report["private_notes"]
        self.assertEqual((pn["warranty_in_description"], pn["warranty_in_notes"] > 10, pn["with_notes"] == pn["jobs"]), (0, True, True))
        text = "\n".join(out) + json.dumps(report)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        self.assertIn("came back with notes", text)

    def test_the_probe_tells_you_when_no_job_returns_a_notes_field(self):
        out = []
        report = load_probe().run_probe(HCPClient(self.base.cfg), limit=5, printer=lambda *a: out.append(" ".join(map(str, a))))
        self.assertEqual(report["private_notes"]["with_notes"], 0)
        self.assertIn("No job came back with a notes field", "\n".join(out))

    def test_the_live_check_warns_when_no_warranty_text_is_found_anywhere(self):
        ds = self.base.fake.dataset
        for j in ds["jobs"]:
            j["description"] = "Service call"
        self.base.live_sync()
        rep = run_live_check(self.base.db, self.base.cfg, self.base.client(), MockGeocoder(), RoadRoutes(None), now=NOW,
                             do_sync=False, printer=lambda *a: None)
        self.assertEqual(rep["jobs"]["warranty_parsed"], 0)
        self.assertTrue(any("No job contains warranty dispatch text" in c["text"] and c["level"] == "WARN" for c in rep["checks"]))

    def test_the_live_check_is_quiet_when_the_dispatch_text_is_found_in_the_notes(self):
        self.notes.live_sync()
        rep = run_live_check(self.notes.db, self.notes.cfg, self.notes.client(), MockGeocoder(), RoadRoutes(None), now=NOW,
                             do_sync=False, printer=lambda *a: None)
        self.assertGreater(rep["jobs"]["warranty_parsed"], 20)
        self.assertFalse(any("No job contains warranty dispatch text" in c["text"] for c in rep["checks"]))


if __name__ == "__main__":
    unittest.main()
