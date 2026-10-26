import time
from decimal import Decimal

from koomyo_erpnext.compat import KoomyoTestCase
from koomyo_erpnext.utils import REDACTED, parse_money, redact, redact_text, verify_signature

# Test vector published on docs.koomyo.com/webhooks/signatures.
DOC_SECRET = "whsec_test_secret"
DOC_T = "1760000000"
DOC_BODY = b'{"id":"evt_1","type":"order.paid"}'
DOC_V1 = "1aeb6b18c8be9c0d7a5c655d8de286865222da311efe0c7b783a5689cfbb9571"


class TestSignature(KoomyoTestCase):
	def test_docs_vector_passes(self):
		ok, reason = verify_signature(DOC_BODY, f"t={DOC_T},v1={DOC_V1}", DOC_SECRET, now=int(DOC_T))
		self.assertTrue(ok, reason)

	def test_wrong_secret_fails(self):
		ok, reason = verify_signature(DOC_BODY, f"t={DOC_T},v1={DOC_V1}", "whsec_other", now=int(DOC_T))
		self.assertFalse(ok)
		self.assertEqual(reason, "signature mismatch")

	def test_changed_body_fails(self):
		ok, _ = verify_signature(DOC_BODY + b" ", f"t={DOC_T},v1={DOC_V1}", DOC_SECRET, now=int(DOC_T))
		self.assertFalse(ok)

	def test_old_and_future_timestamps_fail(self):
		header = f"t={DOC_T},v1={DOC_V1}"
		self.assertFalse(verify_signature(DOC_BODY, header, DOC_SECRET, now=int(DOC_T) + 301)[0])
		self.assertFalse(verify_signature(DOC_BODY, header, DOC_SECRET, now=int(DOC_T) - 301)[0])
		self.assertTrue(verify_signature(DOC_BODY, header, DOC_SECRET, now=int(DOC_T) + 300)[0])

	def test_default_clock_rejects_the_old_docs_vector(self):
		self.assertGreater(time.time(), int(DOC_T) + 300)
		self.assertFalse(verify_signature(DOC_BODY, f"t={DOC_T},v1={DOC_V1}", DOC_SECRET)[0])

	def test_any_of_several_v1_values(self):
		header = f"t={DOC_T},v1={'0' * 64},v1={DOC_V1}"
		self.assertTrue(verify_signature(DOC_BODY, header, DOC_SECRET, now=int(DOC_T))[0])

	def test_malformed_headers(self):
		for header in (None, "", "v1=" + DOC_V1, f"t=abc,v1={DOC_V1}", f"t={DOC_T}", "garbage"):
			ok, reason = verify_signature(DOC_BODY, header, DOC_SECRET, now=int(DOC_T))
			self.assertFalse(ok, header)
			self.assertEqual(reason, "malformed signature header")

	def test_no_secret(self):
		self.assertFalse(verify_signature(DOC_BODY, f"t={DOC_T},v1={DOC_V1}", "", now=int(DOC_T))[0])


class TestRedaction(KoomyoTestCase):
	def test_secret_keys_any_depth(self):
		out = redact(
			{
				"Authorization": "Bearer kyo_live_abc",
				"nested": [{"secret": "whsec_x", "api_key": "k", "ok": 1}],
			}
		)
		self.assertEqual(out["Authorization"], REDACTED)
		self.assertEqual(out["nested"][0]["secret"], REDACTED)
		self.assertEqual(out["nested"][0]["api_key"], REDACTED)
		self.assertEqual(out["nested"][0]["ok"], 1)

	def test_secrets_inside_text(self):
		text = "call failed Authorization: Bearer kyo_live_Omaq4f2L0wq8 secret whsec_43c22f sig v1=9ea4d5ea5a7eb464298cd4"
		clean = redact_text(text)
		for leak in ("kyo_live_Omaq", "whsec_43c22f", "9ea4d5ea5a7eb4"):
			self.assertNotIn(leak, clean)

	def test_private_customer_fields_dropped(self):
		event = {
			"data": {
				"object": "customer",
				"id": "c1",
				"name": "A",
				"notes": "allergic to nuts",
				"tags": ["vip"],
				"do_not_contact": True,
			},
			"order": {"customer": {"id": "c2", "notes": "x", "allergies": "y"}, "notes": "extra cheese"},
		}
		out = redact(event)
		self.assertEqual(out["data"], {"object": "customer", "id": "c1", "name": "A"})
		self.assertEqual(out["order"]["customer"], {"id": "c2"})
		# Order notes are not customer data and stay.
		self.assertEqual(out["order"]["notes"], "extra cheese")


class TestMoney(KoomyoTestCase):
	def test_parse_exact(self):
		self.assertEqual(parse_money("724.50"), Decimal("724.50"))
		self.assertEqual(parse_money("-723.50"), Decimal("-723.50"))
		self.assertEqual(parse_money("125.0000000"), Decimal("125"))
		total = sum((parse_money(x) for x in ("0.10", "0.20")), Decimal("0"))
		self.assertEqual(total, Decimal("0.30"))

	def test_rejects_non_text(self):
		for bad in (724.5, 1, True, None, "", "1,000.00", "1e3", " 1.00", "abc", "NaN"):
			with self.assertRaises(ValueError, msg=repr(bad)):
				parse_money(bad)
