import frappe

from koomyo_erpnext.sync import events
from koomyo_erpnext.tests.fake_koomyo import error_response
from koomyo_erpnext.tests.helpers import (
	BASE_A,
	COMPANY_A,
	COMPANY_B,
	KEY_A,
	SECRET_A,
	TENANT_A,
	KoomyoIntegrationTestCase,
	ensure_company,
	leaf,
	make_connection,
)

BRANCH_ID = "90000000-0000-4000-8000-000000000001"


COMPANY_C = "_Koomyo Test C"


class TestConnection(KoomyoIntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_company(COMPANY_C, "_KTC")
		frappe.db.commit()

	def fresh(self, name="Fresh", company=COMPANY_C, base=BASE_A):
		return make_connection(name, company, base, KEY_A, SECRET_A, tested=False)

	def test_test_connection_fills_restaurant_and_scopes(self):
		conn = self.fresh()
		with self.koomyo():
			result = conn.test_connection()
		conn.reload()
		self.assertTrue(result["ok"], result)
		self.assertEqual(conn.status, "Connected")
		self.assertEqual(conn.tenant_id, TENANT_A)
		self.assertEqual(conn.restaurant_name, "Slice of Heaven Test Cafe")
		self.assertEqual(conn.koomyo_timezone, "Asia/Karachi")
		self.assertIn("events.read", conn.key_scopes)
		self.assertEqual(conn.missing_scopes, "")
		self.assertEqual(result["branches"][0]["id"], BRANCH_ID)
		self.assertFalse(result["branches"][0]["mapped"])
		self.assertTrue(any("not mapped" in w for w in result["warnings"]))

	def test_missing_scopes_shown_and_block(self):
		self.fake_a.account["key"]["scopes"] = ["orders.read", "catalog.read"]
		conn = self.fresh()
		with self.koomyo():
			result = conn.test_connection()
		conn.reload()
		self.assertFalse(result["ok"])
		self.assertEqual(conn.status, "Needs Attention")
		self.assertIn("events.read", conn.missing_scopes)

	def test_handler_scopes_are_required(self):
		events.HANDLER_SCOPES["shift.opened"] = ("shifts.read",)
		self.addCleanup(events.HANDLER_SCOPES.pop, "shift.opened", None)
		self.fake_a.account["key"]["scopes"] = ["events.read", "catalog.read"]
		conn = self.fresh()
		with self.koomyo():
			conn.test_connection()
		conn.reload()
		self.assertEqual(conn.missing_scopes, "shifts.read")

	def test_currency_mismatch_refused(self):
		self.fake_a.account["currency"] = "USD"
		conn = self.fresh()
		with self.koomyo():
			result = conn.test_connection()
		self.assertFalse(result["ok"])
		self.assertIn("USD", result["error"])
		conn.reload()
		conn.enabled = 1
		self.assertRaises(frappe.ValidationError, conn.save)

	def test_revoked_key(self):
		self.fake_a.scripted["/account"] = [error_response(401, "invalid_api_key")]
		conn = self.fresh()
		with self.koomyo():
			result = conn.test_connection()
		conn.reload()
		self.assertFalse(result["ok"])
		self.assertEqual(conn.status, "Needs Attention")
		self.assertIn("revoked", conn.status_message)
		self.assertNotIn(KEY_A, conn.status_message)

	def test_enable_needs_a_test(self):
		conn = self.fresh()
		conn.enabled = 1
		self.assertRaises(frappe.ValidationError, conn.save)

	def test_new_key_needs_a_new_test(self):
		conn = frappe.get_doc("Koomyo Connection", self.conn_a.name)
		conn.api_key = "kyo_live_NEWKEY"
		conn.save()
		self.assertEqual((conn.status, conn.enabled), ("Not Tested", 0))

	def test_same_restaurant_same_company_refused(self):
		conn = self.fresh(company=COMPANY_A)
		with self.koomyo():
			self.assertRaises(frappe.ValidationError, conn.test_connection)

	def test_same_restaurant_other_company_branches_may_not_overlap(self):
		self.conn_a.append(
			"branch_map",
			{
				"koomyo_branch_id": BRANCH_ID,
				"cost_center": leaf("Cost Center", COMPANY_A),
				"warehouse": leaf("Warehouse", COMPANY_A),
			},
		)
		self.conn_a.save()
		conn = make_connection("Test C", COMPANY_C, BASE_A, KEY_A, SECRET_A, tenant_id=TENANT_A)
		conn.append(
			"branch_map",
			{
				"koomyo_branch_id": BRANCH_ID,
				"cost_center": leaf("Cost Center", COMPANY_C),
				"warehouse": leaf("Warehouse", COMPANY_C),
			},
		)
		self.assertRaises(frappe.ValidationError, conn.save)
		conn.reload()
		conn.append(
			"branch_map",
			{
				"koomyo_branch_id": "another-branch",
				"cost_center": leaf("Cost Center", COMPANY_C),
				"warehouse": leaf("Warehouse", COMPANY_C),
			},
		)
		conn.save()

	def test_maps_cannot_cross_companies(self):
		conn = frappe.get_doc("Koomyo Connection", self.conn_a.name)
		conn.append(
			"branch_map",
			{
				"koomyo_branch_id": BRANCH_ID,
				"cost_center": leaf("Cost Center", COMPANY_B),
				"warehouse": leaf("Warehouse", COMPANY_A),
			},
		)
		self.assertRaises(frappe.ValidationError, conn.save)
		conn.branch_map[0].cost_center = leaf("Cost Center", COMPANY_A)
		conn.branch_map[0].warehouse = leaf("Warehouse", COMPANY_B)
		self.assertRaises(frappe.ValidationError, conn.save)

	def test_branch_mapped_twice_refused(self):
		conn = frappe.get_doc("Koomyo Connection", self.conn_a.name)
		for _ in range(2):
			conn.append(
				"branch_map",
				{
					"koomyo_branch_id": BRANCH_ID,
					"cost_center": leaf("Cost Center", COMPANY_A),
					"warehouse": leaf("Warehouse", COMPANY_A),
				},
			)
		self.assertRaises(frappe.ValidationError, conn.save)

	def test_webhook_address_uses_random_token(self):
		self.assertEqual(len(self.conn_a.webhook_token), 32)
		self.assertNotEqual(self.conn_a.webhook_token, self.conn_b.webhook_token)
		self.assertIn(f"koomyo_erpnext.api.webhook?c={self.conn_a.webhook_token}", self.conn_a.webhook_url)
		self.assertNotIn(COMPANY_A, self.conn_a.webhook_url)

	def test_secrets_stored_encrypted(self):
		raw = frappe.db.sql(
			"select api_key, webhook_secret from `tabKoomyo Connection` where name=%s", self.conn_a.name
		)[0]
		self.assertNotIn(KEY_A, raw)
		self.assertNotIn(SECRET_A, raw)
		self.assertEqual(self.conn_a.get_password("api_key"), KEY_A)

	def test_bad_base_url_refused(self):
		conn = frappe.get_doc("Koomyo Connection", self.conn_a.name)
		conn.base_url = "http://koomyo.example.test/api/public/v1"
		self.assertRaises(frappe.ValidationError, conn.save)
