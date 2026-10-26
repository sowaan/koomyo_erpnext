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


def use_test_handler(testcase, event_type: str, handler, scopes=(), switch=None):
	"""Swap in a handler for one test; the real dispatch table is restored afterwards."""
	from koomyo_erpnext.sync import events

	for table, value in (
		(events.DISPATCH, handler),
		(events.HANDLER_SCOPES, tuple(scopes)),
		(events.HANDLER_SWITCH, switch),
	):
		patcher = patch.dict(table, {event_type: value})
		patcher.start()
		testcase.addCleanup(patcher.stop)


def ensure_basics():
	"""What the ERPNext setup wizard makes on a real site: groups, UOMs, modes of payment, a fiscal year."""
	if not frappe.db.exists("Item Group", "All Item Groups"):
		from erpnext.setup.setup_wizard.operations.install_fixtures import install

		install("Pakistan")
	if not frappe.db.exists("Fiscal Year", "2026"):
		frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": "2026",
				"year_start_date": "2026-01-01",
				"year_end_date": "2026-12-31",
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Customer Group", "_Koomyo Customers"):
		frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": "_Koomyo Customers",
				"parent_customer_group": "All Customer Groups",
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Territory", "_Koomyo Territory"):
		frappe.get_doc(
			{
				"doctype": "Territory",
				"territory_name": "_Koomyo Territory",
				"parent_territory": "All Territories",
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Customer", "_Koomyo Walk-in"):
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "_Koomyo Walk-in",
				"customer_group": "_Koomyo Customers",
				"territory": "_Koomyo Territory",
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Price List", "_Koomyo Selling"):
		frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": "_Koomyo Selling",
				"currency": "PKR",
				"selling": 1,
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Item Group", "_Koomyo Menu"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "_Koomyo Menu",
				"parent_item_group": "All Item Groups",
			}
		).insert(ignore_permissions=True)


def ensure_account(company: str, abbr: str, name: str, parent: str, account_type: str = "") -> str:
	account = f"{name} - {abbr}"
	if not frappe.db.exists("Account", account):
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": name,
				"company": company,
				"parent_account": f"{parent} - {abbr}",
				"account_type": account_type,
			}
		).insert(ignore_permissions=True)
	return account


def sales_accounts(company: str, abbr: str) -> dict:
	return {
		"income": f"Sales - {abbr}",
		"tax": ensure_account(company, abbr, "Sales Tax", "Duties and Taxes", "Tax"),
		"cash": f"Cash - {abbr}",
		"bank": ensure_account(company, abbr, "Card Clearing", "Bank Accounts", "Bank"),
		"fees": f"Commission on Sales - {abbr}",
		"delivery": ensure_account(company, abbr, "Delivery Income", "Direct Income"),
		"markup": ensure_account(company, abbr, "Channel Markup Income", "Direct Income"),
		"payment_adj": f"Sales Expenses - {abbr}",
		"cost_center": f"Main - {abbr}",
		"warehouse": f"Stores - {abbr}",
	}


def configure_sales(conn, abbr: str, branch_id: str = "90000000-0000-4000-8000-000000000001", **extra):
	"""Switch sales and refunds on with complete maps, as an accountant would."""
	acc = sales_accounts(conn.company, abbr)
	conn.reload()
	conn.update(
		{
			"sync_sales": 1,
			"sync_refunds": 1,
			"walk_in_customer": "_Koomyo Walk-in",
			"menu_item_group": "_Koomyo Menu",
			"income_account": acc["income"],
			"price_list": "_Koomyo Selling",
			"delivery_fee_account": acc["delivery"],
			"channel_markup_account": acc["markup"],
			"payment_adjustment_account": acc["payment_adj"],
		}
	)
	conn.set("branch_map", [])
	conn.append(
		"branch_map",
		{"koomyo_branch_id": branch_id, "cost_center": acc["cost_center"], "warehouse": acc["warehouse"]},
	)
	conn.set("payment_map", [])
	conn.append("payment_map", {"koomyo_method": "cash", "mode_of_payment": "Cash", "account": acc["cash"]})
	conn.append(
		"payment_map",
		{
			"koomyo_method": "card",
			"mode_of_payment": "Credit Card",
			"account": acc["bank"],
			"fee_percent": 2,
			"fee_account": acc["fees"],
		},
	)
	conn.set("tax_map", [])
	conn.append("tax_map", {"tax_rate": "", "account_head": acc["tax"]})
	conn.update(extra)
	conn.save(ignore_permissions=True)
	frappe.db.commit()
	return acc


def gl(voucher_type: str, voucher_no: str) -> dict:
	"""{account: debit - credit} as exact Decimals, for checking postings to the cent."""
	from decimal import Decimal

	out = {}
	for row in frappe.get_all(
		"GL Entry",
		filters={"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 0},
		fields=["account", "debit", "credit"],
	):
		out[row.account] = (
			out.get(row.account, Decimal(0)) + Decimal(str(row.debit)) - Decimal(str(row.credit))
		)
	return {k: v.quantize(Decimal("0.01")) for k, v in out.items() if v}


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


KOOMYO_DOCTYPES_TO_CLEAN = ("Payment Entry", "Sales Invoice", "Journal Entry")


def hard_delete(doctype: str, name: str):
	"""Test-site clean-up only: remove a (possibly submitted) document and its child rows."""
	for table in frappe.get_meta(doctype).get_table_fields():
		frappe.db.delete(table.options, {"parent": name, "parenttype": doctype})
	frappe.db.delete(doctype, {"name": name})


def clean_koomyo_data():
	"""Remove everything a test made from Koomyo data (documents, GL rows, items, logs)."""
	for doctype in KOOMYO_DOCTYPES_TO_CLEAN:
		for name in frappe.get_all(
			doctype, filters={"koomyo_connection": ("is", "set")}, pluck="name", order_by="creation desc"
		):
			frappe.db.delete("GL Entry", {"voucher_type": doctype, "voucher_no": name})
			frappe.db.delete("Payment Ledger Entry", {"voucher_type": doctype, "voucher_no": name})
			frappe.db.delete("Payment Ledger Entry", {"against_voucher_no": name})
			frappe.db.delete("Comment", {"reference_doctype": doctype, "reference_name": name})
			hard_delete(doctype, name)
	for name in frappe.get_all("Item", filters={"koomyo_connection": ("is", "set")}, pluck="name"):
		frappe.delete_doc("Item", name, force=True, ignore_permissions=True)
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
		ensure_basics()
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
