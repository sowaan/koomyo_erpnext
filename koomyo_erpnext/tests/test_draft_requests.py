"""Keep Unapproved Koomyo Requests as Drafts: a draft Material Request that follows its Koomyo request."""

import frappe
from frappe.utils import flt

from koomyo_erpnext.sync import events, purchase_requests
from koomyo_erpnext.tests.test_purchase_requests import (
	CHEESE,
	LINE_1,
	REQUEST_ID,
	RequestCase,
	line,
	make_request,
)


def waiting(**changes):
	return make_request(
		"submitted",
		"2026-10-26T09:00:00Z",
		lines=[line(LINE_1, CHEESE, "8.000", None, status="submitted")],
		awaiting_branch_approval=True,
		**changes,
	)


class TestDraftRequests(RequestCase):
	def setUp(self):
		super().setUp()
		self.conn_a.reload()
		self.conn_a.keep_draft_requests = 1
		self.conn_a.save()

	def test_a_request_with_nothing_approved_is_a_draft_with_the_asked_quantity(self):
		log = self.send(waiting(), "purchase_request.submitted")
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		mr = self.mr()
		self.assertEqual(mr.docstatus, 0)
		(row,) = mr.items
		self.assertEqual((flt(row.qty, 3), row.koomyo_line_id), (8, LINE_1))
		notes = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Material Request", "reference_name": mr.name},
			pluck="content",
		)
		self.assertTrue([n for n in notes if "Draft in Koomyo" in n])
		self.assertEqual(
			[r for r in self.fake_a.refs if r["entity_type"] == "purchase_request"],
			[],
			"no reference is written back for a draft",
		)

	def test_a_second_copy_changes_nothing(self):
		self.send(waiting(), "purchase_request.submitted")
		name = self.mr().name
		self.send(waiting(), "purchase_request.updated", eid="again")
		self.assertEqual(self.mr().name, name)
		self.assertEqual(
			len(frappe.get_all("Material Request", filters={"koomyo_connection": self.conn_a.name})), 1
		)

	def test_a_changed_asked_quantity_rebuilds_the_same_draft(self):
		self.send(waiting(), "purchase_request.submitted")
		name = self.mr().name
		edited = make_request(
			"submitted",
			"2026-10-26T09:30:00Z",
			lines=[line(LINE_1, CHEESE, "10.000", None, status="submitted")],
		)
		log = self.send(edited, "purchase_request.updated")
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		mr = self.mr()
		self.assertEqual((mr.name, mr.docstatus, flt(mr.items[0].qty, 3)), (name, 0, 10))

	def test_approval_submits_the_same_document_at_the_approved_quantity(self):
		self.send(waiting(), "purchase_request.submitted")
		name = self.mr().name
		log = self.send(make_request(), "purchase_request.approved")
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		mr = self.mr()
		self.assertEqual((mr.name, mr.docstatus, flt(mr.items[0].qty, 3)), (name, 1, 6))
		self.assertEqual(len([r for r in self.fake_a.refs if r["entity_type"] == "purchase_request"]), 1)

	def test_rejected_or_cancelled_in_koomyo_deletes_the_draft(self):
		self.send(waiting(), "purchase_request.submitted")
		name = self.mr().name
		gone = make_request(
			"rejected",
			"2026-10-26T10:00:00Z",
			lines=[line(LINE_1, CHEESE, "8.000", None, status="rejected")],
		)
		log = self.send(gone, "purchase_request.rejected")
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertIn("was deleted", log.message)
		self.assertFalse(frappe.db.exists("Material Request", name))

	def test_deleted_in_koomyo_is_found_by_the_safety_net_and_deleted(self):
		self.send(waiting(), "purchase_request.submitted")
		name = self.mr().name
		self.fake_a.lists["/purchase-requests"] = []  # not listed and GET answers 404
		with self.koomyo():
			summary = purchase_requests.sync_purchase_requests(self.conn_a.name)
			events.process_pending(self.conn_a.name)
		self.assertEqual(summary["queued"], 1)
		self.assertFalse(frappe.db.exists("Material Request", name))
		(log,) = [l for l in self.logs(self.conn_a.name) if l.event_type == purchase_requests.DRAFT_GONE]
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))

	def test_a_draft_that_still_exists_is_left_alone(self):
		self.send(waiting(), "purchase_request.submitted")
		self.fake_a.lists["/purchase-requests"] = [waiting()]
		with self.koomyo():
			purchase_requests.sync_purchase_requests(self.conn_a.name)
			events.process_pending(self.conn_a.name)
		self.assertTrue(self.mr())
		self.assertEqual(self.mr().docstatus, 0)

	def test_it_cannot_be_submitted_by_hand(self):
		self.send(waiting(), "purchase_request.submitted")
		with self.assertRaises(frappe.ValidationError):
			self.mr().submit()
		self.assertEqual(self.mr().docstatus, 0)

	def test_nothing_is_sent_to_koomyo_for_a_draft(self):
		self.send(waiting(), "purchase_request.submitted")
		# only the ingredient's own link (made when the Item is first created, as always), nothing of the request
		self.assertEqual(
			[w["path"] for w in self.fake_a.writes if "purchase" in w["path"]],
			[],
		)

	def test_off_by_default_still_skips(self):
		self.conn_a.reload()
		self.conn_a.keep_draft_requests = 0
		self.conn_a.save()
		log = self.send(waiting(), "purchase_request.submitted")
		self.assertEqual(log.status, "Skipped")
		self.assertIsNone(self.mr())
