import frappe
import requests

from koomyo_erpnext.client import KoomyoClient, validate_base_url
from koomyo_erpnext.compat import KoomyoTestCase
from koomyo_erpnext.exceptions import KoomyoAPIError
from koomyo_erpnext.tests.fake_koomyo import FakeKoomyo, FakeResponse, error_response, patch_koomyo

BASE = "https://koomyo.example.test/api/public/v1"
KEY = "kyo_live_SECRETKEYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class TestClient(KoomyoTestCase):
	def setUp(self):
		self.fake = FakeKoomyo(BASE)
		self.sleeps = []

	def client(self, **kw):
		return KoomyoClient(BASE, KEY, sleep=self.sleeps.append, **kw)

	def test_auth_header_and_account(self):
		with patch_koomyo(self.fake):
			account = self.client().get_account()
		self.assertEqual(account["name"], "Slice of Heaven Test Cafe")
		self.assertEqual(self.fake.calls[0]["headers"]["Authorization"], f"Bearer {KEY}")

	def test_key_never_in_repr_or_errors(self):
		self.fake.scripted["/account"] = [error_response(401, "invalid_api_key", f"bad key {KEY}")]
		with patch_koomyo(self.fake), self.assertRaises(KoomyoAPIError) as ctx:
			client = self.client()
			self.assertNotIn(KEY, repr(client))
			client.get_account()
		self.assertNotIn(KEY, str(ctx.exception))
		self.assertEqual(ctx.exception.code, "invalid_api_key")
		self.assertTrue(ctx.exception.is_key_problem)
		self.assertEqual(len(self.fake.calls), 1, "401 must not be retried")

	def test_429_waits_retry_after_then_succeeds(self):
		self.fake.scripted["/account"] = [error_response(429, "rate_limited", headers={"Retry-After": "6"})]
		with patch_koomyo(self.fake):
			self.client().get_account()
		self.assertEqual(len(self.fake.calls), 2)
		self.assertGreaterEqual(self.sleeps[0], 6)
		self.assertLess(self.sleeps[0], 7)

	def test_long_retry_after_is_handed_back(self):
		self.fake.scripted["/account"] = [error_response(429, "rate_limited", headers={"Retry-After": "120"})]
		with patch_koomyo(self.fake), self.assertRaises(KoomyoAPIError) as ctx:
			self.client().get_account()
		self.assertEqual(ctx.exception.retry_after, 120)
		self.assertTrue(ctx.exception.is_retryable)
		self.assertEqual(self.sleeps, [])

	def test_5xx_retried_with_backoff_then_gives_up(self):
		self.fake.scripted["/account"] = [error_response(500, "internal_error") for _ in range(4)]
		with patch_koomyo(self.fake), self.assertRaises(KoomyoAPIError) as ctx:
			self.client().get_account()
		self.assertEqual(len(self.fake.calls), 4)
		self.assertEqual(len(self.sleeps), 3)
		self.assertEqual(ctx.exception.status, 500)

	def test_4xx_not_retried(self):
		for status, code in (
			(400, "invalid_parameter"),
			(403, "missing_scope"),
			(404, "not_found"),
			(409, "already_received"),
			(422, "idempotency_key_reused"),
		):
			self.fake.calls.clear()
			self.fake.scripted["/account"] = [error_response(status, code)]
			with patch_koomyo(self.fake), self.assertRaises(KoomyoAPIError):
				self.client().get_account()
			self.assertEqual(len(self.fake.calls), 1, code)

	def test_409_request_in_progress_retried(self):
		self.fake.scripted["/account"] = [error_response(409, "request_in_progress")]
		with patch_koomyo(self.fake):
			self.client().get_account()
		self.assertEqual(len(self.fake.calls), 2)

	def test_network_error_retried_then_raised(self):
		self.fake.down = True
		with patch_koomyo(self.fake), self.assertRaises(KoomyoAPIError) as ctx:
			self.client().get_account()
		self.assertEqual(ctx.exception.code, "network_error")
		self.assertEqual(len(self.fake.calls), 4)

	def test_bad_shapes_rejected(self):
		cases = [
			FakeResponse(200, raw_text="<html>"),
			FakeResponse(200, ["not", "an", "object"]),
			FakeResponse(200, {"object": "account", "name": "x"}),
			FakeResponse(302, {}, {"Location": "https://elsewhere"}),
		]
		for response in cases:
			self.fake.scripted["/account"] = [response]
			with patch_koomyo(self.fake), self.assertRaises(KoomyoAPIError) as ctx:
				self.client().get_account()
			self.assertIn(ctx.exception.code, ("bad_response", "unexpected_redirect"))

	def test_cursor_passed_back_untouched(self):
		cursor = "MjAyNi0xMC0xMlQxNjo1NDoxNC4xMjM0NTZafDlhYmM=="
		self.fake.scripted["/branches"] = [
			FakeResponse(
				200, {"object": "list", "data": [{"id": "1"}], "has_more": True, "next_cursor": cursor}
			),
			FakeResponse(
				200, {"object": "list", "data": [{"id": "2"}], "has_more": False, "next_cursor": None}
			),
		]
		with patch_koomyo(self.fake):
			rows = self.client().list_branches()
		self.assertEqual([r["id"] for r in rows], ["1", "2"])
		self.assertNotIn("cursor", self.fake.calls[0]["params"])
		self.assertEqual(self.fake.calls[1]["params"]["cursor"], cursor)

	def test_events_page_uses_after(self):
		self.fake.events = []
		self.fake.add_event("event_shift_opened", seq=5, id="e5")
		with patch_koomyo(self.fake):
			page = self.client().get_events(after=4, types=["shift.opened"])
		self.assertEqual(page["next_after"], 5)
		self.assertEqual(self.fake.calls[0]["params"], {"after": 4, "types": "shift.opened", "limit": 500})


class TestBaseURL(KoomyoTestCase):
	def test_valid(self):
		self.assertEqual(validate_base_url(BASE + "/"), BASE)
		self.assertEqual(
			validate_base_url("http://localhost:8080/api/public/v1"), "http://localhost:8080/api/public/v1"
		)

	def test_invalid(self):
		for url in (
			"http://koomyo.example.test/api/public/v1",
			"https://user:pw@koomyo.example.test/api/public/v1",
			"https://koomyo.example.test/",
			"ftp://koomyo.example.test/api/public/v1",
			"https://koomyo.example.test/api/public/v1?x=1",
			"",
		):
			with self.assertRaises(frappe.ValidationError, msg=url):
				validate_base_url(url)
