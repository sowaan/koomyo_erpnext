"""Slice 7: the dashboard's numbers, and that it shows only what the user may see."""

import frappe

from koomyo_erpnext.dashboard import get_dashboard
from koomyo_erpnext.tests.helpers import COMPANY_A, COMPANY_B, KoomyoIntegrationTestCase
from koomyo_erpnext.tests.test_permissions import USER


def log(conn, company, event_id, status, error_class=""):
	frappe.get_doc(
		{
			"doctype": "Koomyo Event Log",
			"connection": conn,
			"company": company,
			"event_id": event_id,
			"event_type": "order.paid",
			"seq": 1,
			"status": status,
			"error_class": error_class,
			"payload": "{}",
		}
	).insert(ignore_permissions=True)


class TestDashboard(KoomyoIntegrationTestCase):
	def setUp(self):
		super().setUp()
		log(self.conn_a.name, COMPANY_A, "a1", "Done")
		log(self.conn_a.name, COMPANY_A, "a2", "Failed", "NeedsMapping")
		log(self.conn_a.name, COMPANY_A, "a3", "Pending")
		log(self.conn_b.name, COMPANY_B, "b1", "Failed", "Fatal")
		self.conn_b.db_set({"status": "Needs Attention", "status_message": "Key revoked"})
		frappe.db.commit()

	def test_numbers(self):
		data = get_dashboard()
		self.assertEqual(
			data["totals"], {"connections": 2, "attention": 1, "failed": 2, "pending": 1, "done_today": 1}
		)
		a = next(c for c in data["connections"] if c["name"] == self.conn_a.name)
		self.assertEqual((a["counts"]["Failed"], a["needs_mapping"], a["last_day"]["Done"]), (1, 1, 1))
		self.assertIsNotNone(a["oldest_pending"])

	def test_other_company_is_hidden(self):
		if not frappe.db.exists("User", USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": USER,
					"first_name": "Accountant A",
					"send_welcome_email": 0,
					"roles": [{"role": "Koomyo Manager"}],
				}
			).insert(ignore_permissions=True)
		frappe.db.delete("User Permission", {"user": USER})
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": USER,
				"allow": "Company",
				"for_value": COMPANY_A,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		frappe.set_user(USER)
		try:
			data = get_dashboard()
		finally:
			frappe.set_user("Administrator")
			frappe.db.delete("User Permission", {"user": USER})
			frappe.db.commit()
		self.assertEqual([c["name"] for c in data["connections"]], [self.conn_a.name])
		self.assertEqual(data["totals"]["failed"], 1)

	def test_guest_refused(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				get_dashboard()
		finally:
			frappe.set_user("Administrator")
