import frappe

from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry_all_failed
from koomyo_erpnext.tests.helpers import COMPANY_A, COMPANY_B, KoomyoIntegrationTestCase

USER = "koomyo.accountant.a@example.test"


class TestPermissions(KoomyoIntegrationTestCase):
	def setUp(self):
		super().setUp()
		if not frappe.db.exists("User", USER):
			frappe.get_doc(
				{"doctype": "User", "email": USER, "first_name": "Accountant A", "send_welcome_email": 0}
			).insert(ignore_permissions=True)
		user = frappe.get_doc("User", USER)
		user.roles = []
		user.append("roles", {"role": "Koomyo Manager"})
		user.save(ignore_permissions=True)
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
		for conn, company in ((self.conn_a, COMPANY_A), (self.conn_b, COMPANY_B)):
			frappe.get_doc(
				{
					"doctype": "Koomyo Event Log",
					"connection": conn.name,
					"company": company,
					"event_id": f"evt-{company}",
					"event_type": "order.paid",
					"seq": 1,
					"status": "Failed",
					"error_class": "Fatal",
					"payload": "{}",
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete("User Permission", {"user": USER})
		super().tearDown()

	def test_user_sees_only_own_company(self):
		frappe.set_user(USER)
		self.assertEqual(frappe.get_list("Koomyo Event Log", pluck="company"), [COMPANY_A])
		self.assertEqual(frappe.get_list("Koomyo Connection", pluck="name"), [self.conn_a.name])
		other = frappe.db.get_value("Koomyo Event Log", {"company": COMPANY_B})
		self.assertFalse(frappe.has_permission("Koomyo Event Log", "read", other))

	def test_retry_all_touches_only_own_company(self):
		frappe.set_user(USER)
		retry_all_failed()
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Koomyo Event Log", {"company": COMPANY_A}, "status"), "Pending")
		self.assertEqual(frappe.db.get_value("Koomyo Event Log", {"company": COMPANY_B}, "status"), "Failed")

	def test_user_cannot_retry_other_company_by_name(self):
		from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry

		other = frappe.db.get_value("Koomyo Event Log", {"company": COMPANY_B})
		frappe.set_user(USER)
		self.assertRaises(frappe.PermissionError, retry, [other])
