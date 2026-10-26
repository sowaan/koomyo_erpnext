"""Shared set-up for the integration tests: two Companies, two connections, signed webhook calls."""

import json
import time
from unittest.mock import patch

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from koomyo_erpnext.compat import KoomyoTestCase
from koomyo_erpnext.tests.fake_koomyo import FakeKoomyo, patch_koomyo
from koomyo_erpnext.utils import compute_signature

COMPANY_A = "_Koomyo Test A"
COMPANY_B = "_Koomyo Test B"
BASE_A = "https://koomyo-a.example.test/api/public/v1"
BASE_B = "https://koomyo-b.example.test/api/public/v1"
SECRET_A = "whsec_test_secret_a"
SECRET_B = "whsec_test_secret_b"
KEY_A = "kyo_live_TESTKEYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
KEY_B = "kyo_live_TESTKEYBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
# Two separate Koomyo restaurants; the fakes reuse every other id from the docs examples.
TENANT_A = "bd0c698a-158d-47a0-9a8c-f4e899f2569e"
TENANT_B = "11111111-2222-4333-8444-555555555555"


def ensure_company(name: str, abbr: str, currency: str = "PKR"):
	if frappe.db.exists("Company", name):
		return
	# Made by the setup wizard on a real site; a bare test site may not have it.
	if not frappe.db.exists("Warehouse Type", "Transit"):
		frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert(ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": "Company",
			"company_name": name,
			"abbr": abbr,
			"default_currency": currency,
			"country": "Pakistan",
			"create_chart_of_accounts_based_on": "Standard Template",
			"chart_of_accounts": "Standard",
		}
	).insert(ignore_permissions=True)


def leaf(doctype: str, company: str) -> str:
	return frappe.get_all(doctype, filters={"company": company, "is_group": 0}, pluck="name", limit=1)[0]


def clean_koomyo_data():
	frappe.db.delete("Koomyo Event Log")
	frappe.db.delete("Koomyo Branch Map")
	frappe.db.delete("Koomyo Connection")
	frappe.db.commit()


def make_connection(name, company, base_url, api_key, secret, *, tested=True, tenant_id=None, enabled=True):
	doc = frappe.get_doc(
		{
			"doctype": "Koomyo Connection",
			"connection_name": name,
			"company": company,
			"base_url": base_url,
			"api_key": api_key,
			"webhook_secret": secret,
			"start_from_date": "2026-10-01",
		}
	).insert(ignore_permissions=True)
	if tested:
		# What a successful Test Connection leaves behind.
		doc.db_set(
			{
				"status": "Connected",
				"tenant_id": tenant_id,
				"restaurant_name": "Test Restaurant",
				"koomyo_currency": "PKR",
				"koomyo_timezone": "Asia/Karachi",
				"enabled": 1 if enabled else 0,
			}
		)
	frappe.db.commit()
	return frappe.get_doc("Koomyo Connection", doc.name)


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
	t = str(int(time.time()) if timestamp is None else timestamp)
	return f"t={t},v1={compute_signature(secret, t, body)}"


def post_webhook(token: str, body: bytes, signature: str | None, event_id: str | None = None):
	"""Call the webhook method the way Frappe would for a POST. Returns (status, text)."""
	from koomyo_erpnext import api

	headers = {"Content-Type": "application/json"}
	if signature is not None:
		headers["Koomyo-Signature"] = signature
	if event_id:
		headers["Koomyo-Event-Id"] = event_id
	builder = EnvironBuilder(
		method="POST",
		path="/api/method/koomyo_erpnext.api.webhook",
		query_string={"c": token},
		data=body,
		headers=headers,
	)
	old_request = getattr(frappe.local, "request", None)
	old_response = getattr(frappe.local, "response", None)
	frappe.local.request = Request(builder.get_environ())
	frappe.local.response = frappe._dict()
	try:
		text = api.webhook()
		status = frappe.local.response.get("http_status_code") or 200
	finally:
		frappe.local.request = old_request
		frappe.local.response = old_response
	return status, text


def event_body(event: dict) -> bytes:
	return json.dumps(event, separators=(",", ":")).encode()


class KoomyoIntegrationTestCase(KoomyoTestCase):
	"""Two Companies, two connections on two fake Koomyo servers that reuse the same ids."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company(COMPANY_A, "_KTA")
		ensure_company(COMPANY_B, "_KTB")
		frappe.db.commit()

	def setUp(self):
		clean_koomyo_data()
		frappe.db.set_single_value("Koomyo Settings", {"alert_email": "", "max_auto_retries": 8})
		self.enqueued = []
		patcher = patch("frappe.enqueue", side_effect=lambda *a, **kw: self.enqueued.append((a, kw)))
		patcher.start()
		self.addCleanup(patcher.stop)
		self.fake_a = FakeKoomyo(BASE_A, tenant_id=TENANT_A)
		self.fake_b = FakeKoomyo(BASE_B, tenant_id=TENANT_B)
		self.conn_a = make_connection("Test A", COMPANY_A, BASE_A, KEY_A, SECRET_A, tenant_id=TENANT_A)
		self.conn_b = make_connection("Test B", COMPANY_B, BASE_B, KEY_B, SECRET_B, tenant_id=TENANT_B)

	def tearDown(self):
		frappe.set_user("Administrator")
		clean_koomyo_data()

	def koomyo(self):
		return patch_koomyo(self.fake_a, self.fake_b)

	def logs(self, connection=None, **filters):
		if connection:
			filters["connection"] = connection
		return frappe.get_all("Koomyo Event Log", filters=filters, fields=["*"], order_by="seq asc")
