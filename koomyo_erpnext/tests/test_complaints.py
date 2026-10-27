"""Complaints -> Issues (docs.koomyo.com/events/complaints). Samples start from the docs examples."""

import copy

import frappe

from koomyo_erpnext.tests.fake_koomyo import load_fixture
from koomyo_erpnext.tests.helpers import TENANT_A
from koomyo_erpnext.tests.test_sales import SalesTestCase

COMPLAINT_ID = "00747ec0-e1e0-4790-8c5e-a4f09653fc3d"


def complaint(**changes):
	data = copy.deepcopy(load_fixture("event_complaint_created")["data"])
	data.update(changes)
	return data


def event(data, event_type="complaint.created", seq=400, eid=None):
	ev = copy.deepcopy(load_fixture("event_complaint_created"))
	ev.update(
		{"type": event_type, "tenant_id": TENANT_A, "seq": seq, "id": eid or f"evt-cmp-{seq}", "data": data}
	)
	ev["entity"] = {"type": "complaint", "id": data["id"]}
	return ev


class TestComplaints(SalesTestCase):
	def setUp(self):
		super().setUp()
		self.conn_a.reload()
		self.conn_a.sync_complaints = 1
		self.conn_a.save()
		self.seq = 400

	def send(self, data, event_type="complaint.created", **kw):
		self.seq += 1
		return self.book(event(data, event_type, self.seq, **kw))

	def issue(self):
		name = frappe.db.get_value(
			"Issue", {"koomyo_connection": self.conn_a.name, "koomyo_id": COMPLAINT_ID}
		)
		return frappe.get_doc("Issue", name) if name else None

	def test_docs_example_becomes_an_issue_without_the_customers_name_or_phone(self):
		log = self.send(complaint())
		self.assertEqual(log.status, "Done", (log.message, log.error[-500:]))
		issue = self.issue()
		self.assertEqual(issue.subject, "CMP-000001: Pizza arrived cold")
		self.assertEqual((issue.status, issue.priority, issue.company), ("Open", "High", self.conn_a.company))
		self.assertEqual(str(issue.opening_date), "2026-10-12")
		self.assertEqual(issue.koomyo_branch, "90000000-0000-4000-8000-000000000001")
		for text in (
			"Delivered 45 minutes late",
			"Wrong item",
			"phone",
			"ORD-PFB-20261012-0001",
			"Photos in Koomyo: 1",
		):
			self.assertIn(text, issue.description)
		blob = " ".join(str(v) for v in issue.as_dict().values())
		self.assertNotIn("Ayesha", blob)
		self.assertNotIn("+923009876501", blob)
		self.assertFalse(issue.customer)

	def test_updates_follow_status_priority_and_resolution_and_replays_change_nothing(self):
		self.send(complaint())
		name = self.issue().name
		for status, expected in (("waiting_customer", "On Hold"), ("in_progress", "Open")):
			self.send(
				complaint(
					status=status, updated_at=f"2026-10-12T18:{'10' if status == 'in_progress' else '00'}:00Z"
				),
				"complaint.updated",
			)
			self.assertEqual(self.issue().status, expected)
		resolved = complaint(
			status="resolved",
			priority="urgent",
			resolution_summary="Refunded the order",
			resolution_cost="2425.40",
			resolved_at="2026-10-12T19:00:00Z",
			updated_at="2026-10-12T19:00:00Z",
		)
		self.assertEqual(self.send(resolved, "complaint.updated").status, "Done")
		issue = self.issue()
		self.assertEqual((issue.name, issue.status, issue.priority), (name, "Resolved", "High"))
		self.assertIn("Refunded the order", issue.resolution_details)
		self.assertIn("2425.40", issue.resolution_details)
		self.send(complaint(status="closed", updated_at="2026-10-13T08:00:00Z"), "complaint.updated")
		self.assertEqual(self.issue().status, "Closed")
		# an older copy arriving late, and a duplicate, change nothing
		self.send(complaint(status="in_progress", updated_at="2026-10-12T17:50:00Z"), "complaint.updated")
		self.assertEqual(self.issue().status, "Closed")
		self.assertEqual(len(frappe.get_all("Issue", filters={"koomyo_id": COMPLAINT_ID})), 1)

	def test_a_synced_customer_is_linked_by_id_only(self):
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "_Koomyo Complainer",
				"customer_group": "_Koomyo Customers",
				"territory": "_Koomyo Territory",
			}
		).insert()
		customer.db_set({"koomyo_connection": self.conn_a.name, "koomyo_id": "cust-1"})
		try:
			self.send(complaint(customer={"id": "cust-1"}))
			self.assertEqual(self.issue().customer, customer.name)
		finally:
			frappe.db.set_value("Customer", customer.name, {"koomyo_connection": None, "koomyo_id": None})
			frappe.delete_doc("Customer", customer.name, force=True)

	def test_koomyos_text_is_escaped(self):
		self.send(complaint(description="<script>alert(1)</script>\nsecond line", summary="x" * 300))
		issue = self.issue()
		self.assertNotIn("<script>", issue.description)
		self.assertIn("&lt;script&gt;", issue.description)
		self.assertLessEqual(len(issue.subject), 140)

	def test_off_by_default_and_unknown_status_is_refused(self):
		self.conn_a.reload()
		self.conn_a.sync_complaints = 0
		self.conn_a.save()
		log = self.send(complaint())
		self.assertEqual(log.status, "Skipped")
		self.assertIn("Sync Complaints", log.message)
		self.conn_a.reload()
		self.conn_a.sync_complaints = 1
		self.conn_a.save()
		self.assertEqual(self.send(complaint(status="exploded"), eid="bad").status, "Failed")

	def test_the_scope_is_asked_for_only_when_it_is_on(self):
		from koomyo_erpnext.sync import events

		self.assertIn("complaints.read", events.required_scopes(self.conn_a))
		self.conn_a.sync_complaints = 0
		self.assertNotIn("complaints.read", events.required_scopes(self.conn_a))
