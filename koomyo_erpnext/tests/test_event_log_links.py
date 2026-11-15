"""The Event Log never blocks deleting a document and shows in its Connections panel."""

import frappe

from koomyo_erpnext import dashboards
from koomyo_erpnext.tests.test_sales import SalesTestCase, order_event


class TestEventLogLinks(SalesTestCase):
	def test_a_document_with_event_log_rows_can_be_deleted_and_the_log_stays(self):
		issue = frappe.get_doc({"doctype": "Issue", "subject": "Koomyo complaint (test)"}).insert(
			ignore_permissions=True
		)
		log = frappe.new_doc("Koomyo Event Log")
		log.update(
			{
				"connection": self.conn_a.name,
				"company": self.conn_a.company,
				"event_id": "evt-del-1",
				"event_type": "complaint.created",
				"seq": 1,
				"received_via": "Feed",
				"event_created": frappe.utils.now_datetime(),
				"payload": "{}",
				"status": "Done",
				"attempts": 1,
				"reference_doctype": "Issue",
				"reference_name": issue.name,
			}
		)
		log.insert(ignore_permissions=True)
		frappe.delete_doc("Issue", issue.name)  # used to fail: "linked with Koomyo Event Log"
		self.assertFalse(frappe.db.exists("Issue", issue.name))
		self.assertTrue(frappe.db.exists("Koomyo Event Log", log.name), "the log row stays")

	def test_the_connections_panel_counts_only_that_documents_log_rows(self):
		self.book(order_event(order_id="dash-1"))
		inv = self.invoice("dash-1")
		data = frappe.get_meta("Sales Invoice").get_dashboard_data()
		self.assertIn("Koomyo Event Log", [i for g in data.transactions for i in g["items"]])
		self.assertEqual(data.non_standard_fieldnames["Koomyo Event Log"], "reference_name")
		self.assertEqual(data.dynamic_links["reference_name"], ["Sales Invoice", "reference_doctype"])
		from frappe.desk.notifications import get_external_links

		count = get_external_links("Koomyo Event Log", inv.name, data)["count"]
		self.assertGreaterEqual(count, 1)
		# the same name under another doctype is not counted
		other = dict(data)
		other["dynamic_links"] = {"reference_name": ["Purchase Order", "reference_doctype"]}
		self.assertEqual(get_external_links("Koomyo Event Log", inv.name, other)["count"], 0)

	def test_every_document_type_has_its_panel_function(self):
		for doctype in dashboards.DOCTYPES:
			name = doctype.lower().replace(" ", "_")
			self.assertTrue(callable(getattr(dashboards, name)), doctype)
			hooked = frappe.get_hooks("override_doctype_dashboards").get(doctype)
			self.assertEqual(hooked, [f"koomyo_erpnext.dashboards.{name}"], doctype)
			self.assertEqual(getattr(dashboards, name)({})["dynamic_links"]["reference_name"][0], doctype)

	def test_the_connection_form_lists_its_event_log_and_backfills(self):
		data = frappe.get_meta("Koomyo Connection").get_dashboard_data()
		linked = {t for g in data.transactions for t in g["items"]}
		self.assertTrue({"Koomyo Event Log", "Koomyo Backfill"} <= linked)

	def test_a_connection_with_log_rows_cannot_be_deleted_and_says_why(self):
		self.book(order_event(order_id="conn-1"))
		with self.assertRaises(frappe.ValidationError) as caught:
			frappe.get_doc("Koomyo Connection", self.conn_a.name).run_method("on_trash")
		self.assertIn("Event Log", str(caught.exception))
