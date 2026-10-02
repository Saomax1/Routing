"""Warranty parser tests. All data is sanitized/fake (see app/hcp/fixtures.py)."""
import os
import unittest

from app.domain.warranty_parser import (
    find_urgency_flags, looks_like_warranty, missing_key_fields, parse_warranty_job,
)
from app.hcp.fixtures import build_ahs_description

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_ahs_job.txt")


class SampleFileTests(unittest.TestCase):
    """The sanitized sample from the project (sample_ahs_job.txt)."""

    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, encoding="utf-8") as f:
            cls.job = parse_warranty_job(f.read())

    def test_identity(self):
        j = self.job
        self.assertTrue(j.is_warranty)
        self.assertEqual(j.warranty_company, "AHS")
        self.assertEqual(j.dispatch_number, "90000001")
        self.assertEqual(j.trade_code, "PLB")
        self.assertEqual(j.service_request_id, "21000001")
        self.assertEqual(j.vendor_id, "1000000")
        self.assertEqual(j.contract_id, "600000001")
        self.assertEqual(j.contract_effective_date, "2026-01-04")

    def test_priority_comes_from_body(self):
        self.assertEqual(self.job.dispatch_priority, "Normal")
        self.assertEqual(self.job.priority_rank, 1)
        self.assertEqual(self.job.header_secondary_code, "NORMAL")
        self.assertIs(self.job.authorization_required, False)

    def test_contact_and_address(self):
        j = self.job
        self.assertEqual(j.contact_name, "Jane Sample")
        self.assertEqual(j.contact_phones, ["4805550101"])
        self.assertEqual((j.street, j.city, j.state, j.zip_code), ("123 W Example St", "Chandler", "AZ", "85226"))
        self.assertEqual(j.full_address, "123 W Example St, Chandler, AZ 85226")

    def test_items_and_flags(self):
        j = self.job
        self.assertEqual(len(j.items), 1)
        self.assertEqual(j.items[0]["name"], "Stoppage")
        self.assertEqual(j.items[0]["problem"], "Not Working")
        self.assertEqual(j.items[0]["status"], "Open")
        self.assertEqual(j.urgency_flags, [])

    def test_payment_and_notices(self):
        j = self.job
        self.assertEqual(j.plan_name, "SHIELDGOLD HOME WARRANTY")
        self.assertEqual(j.payment_type, "PREPAY")
        self.assertTrue(j.do_not_collect_service_fee)
        self.assertTrue(j.completion_date_required)
        self.assertTrue(j.recall_applies)
        self.assertEqual((j.total, j.paid, j.remaining), (100.0, 0.0, 100.0))

    def test_links_decoded(self):
        j = self.job
        self.assertIn("&dispatch=90000001&svcReq=21000001", j.authorization_link)  # &amp; decoded
        self.assertNotIn("&amp;", j.authorization_link)
        self.assertEqual(j.dispatch_me_links, ["http://dispa.me/AAAA1111", "http://dispa.me/BBBB2222"])

    def test_no_warnings(self):
        self.assertEqual(self.job.parse_warnings, [])


class PriorityTests(unittest.TestCase):
    def test_expedited_header_still_says_normal(self):
        """Real-world quirk: header suffix stayed NORMAL on an Expedited job."""
        j = parse_warranty_job(build_ahs_description(priority="Expedited", header_code="NORMAL"))
        self.assertEqual(j.dispatch_priority, "Expedited")
        self.assertEqual(j.priority_rank, 2)
        self.assertEqual(j.header_secondary_code, "NORMAL")

    def test_emergency(self):
        j = parse_warranty_job(build_ahs_description(priority="Emergency", header_code="NORMAL"))
        self.assertEqual(j.dispatch_priority, "Emergency")
        self.assertEqual(j.priority_rank, 3)

    def test_unknown_priority_warns(self):
        j = parse_warranty_job(build_ahs_description(priority="Weird"))
        self.assertEqual(j.dispatch_priority, "Weird")
        self.assertEqual(j.priority_rank, 0)
        self.assertTrue(any("Unrecognised" in w for w in j.parse_warnings))

    def test_authorization_required_true(self):
        j = parse_warranty_job(build_ahs_description(autho_required=True))
        self.assertIs(j.authorization_required, True)


class ItemTests(unittest.TestCase):
    def test_area_of_home(self):
        j = parse_warranty_job(build_ahs_description(
            items=[{"name": "Faucet", "problem": "Dripping", "area": "Kitchen"}]))
        self.assertEqual(j.items[0]["area_of_home"], "Kitchen")

    def test_multiple_items(self):
        j = parse_warranty_job(build_ahs_description(items=[
            {"name": "Faucet", "problem": "Dripping", "area": "Kitchen"},
            {"name": "Toilet", "problem": "Running constantly", "area": "Master Bath", "status": "Pending"},
        ]))
        self.assertEqual([i["name"] for i in j.items], ["Faucet", "Toilet"])
        self.assertEqual(j.items[1]["area_of_home"], "Master Bath")
        self.assertEqual(j.items[1]["status"], "Pending")
        self.assertEqual(j.items[1]["problem"], "Running constantly")

    def test_urgency_flags_from_problem_text(self):
        j = parse_warranty_job(build_ahs_description(
            items=[{"name": "Water Leak", "problem": "Leak causing secondary damage"}]))
        self.assertIn("leak", j.urgency_flags)
        self.assertIn("secondary damage", j.urgency_flags)

    def test_urgency_keywords_are_whole_words(self):
        # "no ac" must not match "no access"; "gas" must not match "gasket"; "leak" must not match "leakage"?
        self.assertEqual(find_urgency_flags("customer says no access to unit, replace gasket"), [])
        self.assertEqual(find_urgency_flags("No AC, smells like gas"), ["no ac", "gas"])
        self.assertEqual(find_urgency_flags("Sewage backup in tub"), ["sewage", "backup"])


class RobustnessTests(unittest.TestCase):
    def test_missing_address_warns_not_crashes(self):
        j = parse_warranty_job(build_ahs_description(include_address=False))
        self.assertIsNone(j.full_address)
        self.assertTrue(any("address" in w.lower() for w in j.parse_warnings))
        self.assertIn("address", missing_key_fields(j))
        self.assertTrue(j.is_warranty)  # still recognised as a warranty job

    def test_street_only_address_is_incomplete(self):
        j = parse_warranty_job(build_ahs_description(city=None, zip_code=None))
        self.assertIsNone(j.full_address)
        self.assertEqual(j.street, "123 W Example St")
        self.assertTrue(any("incomplete" in w for w in j.parse_warnings))

    def test_empty_and_garbage_input(self):
        for raw in ("", None, "just a plain note: replace disposal", "\x00\x01 ### ??"):
            j = parse_warranty_job(raw)
            self.assertFalse(j.is_warranty)
            self.assertIsInstance(j.parse_warnings, list)

    def test_non_warranty_text_is_not_flagged(self):
        self.assertFalse(looks_like_warranty("Customer wants a tankless water heater quote."))
        self.assertFalse(looks_like_warranty(None))
        self.assertTrue(looks_like_warranty(build_ahs_description()))

    def test_unencoded_ampersands_also_parse(self):
        j = parse_warranty_job(build_ahs_description(entity_encoded=False))
        self.assertEqual(j.service_request_id, "21000001")
        self.assertEqual(j.vendor_id, "1000000")

    def test_trade_code_falls_back_to_auth_link(self):
        text = build_ahs_description(trade="HVAC").replace("90000001 HVAC Normal:NORMAL", "")
        j = parse_warranty_job(text)
        self.assertEqual(j.trade_code, "HVAC")

    def test_money_with_commas_and_bold_markers(self):
        text = build_ahs_description().replace("**Total: $100", "**Total:** $1,100.50")
        self.assertEqual(parse_warranty_job(text).total, 1100.50)

    def test_contract_contact_fallback(self):
        text = build_ahs_description().replace("(Dispatch Contact)", "(Someone)")
        self.assertEqual(parse_warranty_job(text).contact_name, "Jane Sample")


if __name__ == "__main__":
    unittest.main()
