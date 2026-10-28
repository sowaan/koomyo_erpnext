"""Section 1 and 4: Koomyo purchase requests -> Material Requests, from the five events and the safety net.

Samples start from the docs examples (fixtures/event_purchase_request_*.json): PR-2026-0012 of the
Gulberg branch, 8 kg of mozzarella asked, 6 kg approved.
"""

import copy

import frappe
from frappe.utils import flt

from koomyo_erpnext.sync import events, purchase_requests
from koomyo_erpnext.tests.fake_koomyo import load_fixture
from koomyo_erpnext.tests.helpers import TENANT_A
from koomyo_erpnext.tests.test_stock import MOZZARELLA, StockTestCase

BRANCH = "90000000-0000-4000-8000-000000000001"
REQUEST_ID = "5d7a1c62-0b1e-4f0e-9d33-2f6f5a4b8e00"
LINE_1 = "5d7a1c62-0b1e-4f0e-9d33-2f6f5a4b8e01"
LINE_2 = "5d7a1c62-0b1e-4f0e-9d33-2f6f5a4b8e02"
CHEESE = {"id": MOZZARELLA, "name": "Mozzarella", "unit": "kg"}
FLOUR = {"id": "5e8b2d73-1c2f-4a1f-8e44-3a7a6b5c9f11", "name": "Flour", "unit": "kg"}


def make_request(status="approved", updated_at="2026-10-26T10:02:00Z", lines=None, **changes):
	card = copy.deepcopy(load_fixture("event_purchase_request_approved")["data"])
	card.update({"id": REQUEST_ID, "status": status, "updated_at": updated_at, "external_refs": {}})
	card["lines"][0].update({"id": LINE_1, "ingredient": dict(CHEESE)})
	if lines is not None:
		card["lines"] = lines
	card.update(changes)
	return card


def line(line_id, ingredient, quantity, approved, status="approved", **extra):
	return {
		"id": line_id,
		"ingredient": dict(ingredient),
		"quantity": quantity,
		"approved_quantity": approved,
		"status": status,
		"needed_by": "2026-11-02",
		"note": None,
		"ordered_quantity": "0.000",
		"received_quantity": "0.000",
		"closed_short": False,
		"reject_reason": None,
		"orders": [],
		**extra,
	}


def request_event(card, event_type, seq, eid=None):
	event = copy.deepcopy(load_fixture("event_purchase_request_approved"))
	event.update(
		{
			"id": eid or f"evt-pr-{seq}",
			"seq": seq,
			"type": event_type,
			"tenant_id": TENANT_A,
			"data": copy.deepcopy(card),
			"entity": {"type": "purchase_request", "id": card["id"]},
		}
	)
	return event


class RequestCase(StockTestCase):
	def setUp(self):
		super().setUp()
		self.conn_a.reload()
		self.conn_a.update({"sync_purchasing": 1, "sync_purchase_requests": 1})
		self.conn_a.save()
		self.seq = 500
		self.fake_a.add_ingredient(FLOUR["id"], "Flour", "kg", "90.0000")

	def tearDown(self):
		for name in frappe.get_all(
			"Material Request", filters={"koomyo_connection": self.conn_a.name}, pluck="name"
		):
			frappe.db.delete("Comment", {"reference_doctype": "Material Request", "reference_name": name})
		super().tearDown()

	def send(self, card, event_type="purchase_request.approved", **kw):
		self.seq += 1
		return self.book(request_event(card, event_type, self.seq, **kw))

	def mr(self, request_id=REQUEST_ID):
		name = frappe.db.get_value(
			"Material Request", {"koomyo_connection": self.conn_a.name, "koomyo_id": request_id}
		)
		return frappe.get_doc("Material Request", name) if name else None


class TestEvents(RequestCase):
	def test_approved_request_becomes_a_material_request_at_the_approved_quantity(self):
		log = self.send(make_request())
		self.assertEqual(log.status, "Done", (log.message, log.error[-500:]))
		mr = self.mr()
		self.assertEqual(
			(mr.docstatus, mr.material_request_type, mr.company), (1, "Purchase", self.conn_a.company)
		)
		(row,) = mr.items
		self.assertEqual((flt(row.qty, 3), row.warehouse, row.koomyo_line_id), (6, "Stores - _KTA", LINE_1))
		self.assertEqual(frappe.db.get_value("Item", row.item_code, "koomyo_id"), MOZZARELLA)
		self.assertEqual(str(mr.schedule_date), "2026-11-02")
		self.assertEqual((mr.koomyo_branch, mr.koomyo_updated_at), (BRANCH, "2026-10-26T10:02:00Z"))
		# the same event again, or the same request from another event, changes nothing
		self.send(make_request(), "purchase_request.updated", eid="again")
		self.assertEqual(
			len(frappe.get_all("Material Request", filters={"koomyo_connection": self.conn_a.name})), 1
		)

	def test_the_name_is_written_back_so_koomyo_can_find_it(self):
		self.send(make_request())
		(ref,) = [r for r in self.fake_a.refs if r["entity_type"] == "purchase_request"]
		self.assertEqual((ref["entity_id"], ref["external_id"]), (REQUEST_ID, self.mr().name))

	def test_a_request_waiting_for_purchasing_creates_nothing(self):
		waiting = make_request(
			"submitted",
			lines=[line(LINE_1, CHEESE, "8.000", None, status="submitted")],
			awaiting_branch_approval=True,
		)
		log = self.send(waiting, "purchase_request.submitted")
		self.assertEqual(log.status, "Skipped")
		self.assertIn("nothing approved yet", log.message)
		self.assertIsNone(self.mr())

	def test_only_approved_lines_are_in_it_and_a_later_approval_rebuilds_it(self):
		first = make_request(
			"submitted",
			lines=[
				line(LINE_1, CHEESE, "8.000", "6.000"),
				line(LINE_2, FLOUR, "20.000", None, status="submitted"),
			],
		)
		self.assertEqual(self.send(first, "purchase_request.updated").status, "Done")
		old = self.mr()
		self.assertEqual(
			[r.koomyo_line_id for r in old.items], [LINE_1], "a line still waiting is not ordered"
		)
		second = make_request(
			"approved",
			"2026-10-26T11:00:00Z",
			lines=[line(LINE_1, CHEESE, "8.000", "6.000"), line(LINE_2, FLOUR, "20.000", "20.000")],
		)
		log = self.send(second, "purchase_request.approved")
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		new = self.mr()
		self.assertNotEqual(new.name, old.name)
		self.assertEqual(sorted(r.koomyo_line_id for r in new.items), [LINE_1, LINE_2])
		self.assertEqual(frappe.db.get_value("Material Request", old.name, "docstatus"), 2)
		self.assertFalse(frappe.db.get_value("Material Request", old.name, "koomyo_id"))

	def test_a_changed_approved_quantity_rebuilds_but_progress_does_not(self):
		self.send(make_request())
		first = self.mr()
		progress = make_request(
			"partly_received",
			"2026-10-27T08:00:00Z",
			lines=[
				line(
					LINE_1,
					CHEESE,
					"8.000",
					"6.000",
					status="partly_received",
					ordered_quantity="6.000",
					received_quantity="2.000",
				)
			],
		)
		self.assertEqual(self.send(progress, "purchase_request.updated").status, "Done")
		self.assertEqual(self.mr().name, first.name, "ordered and received progress never rebuild it")
		self.assertEqual(self.mr().koomyo_updated_at, "2026-10-27T08:00:00Z")
		changed = make_request(
			"approved",
			"2026-10-28T08:00:00Z",
			lines=[line(LINE_1, CHEESE, "8.000", "7.000")],
		)
		self.assertEqual(self.send(changed, "purchase_request.updated").status, "Done")
		self.assertEqual(flt(self.mr().items[0].qty, 3), 7)

	def test_a_change_after_erpnext_ordered_is_flagged_not_forced(self):
		self.send(make_request())
		mr = self.mr()
		# what an order from it leaves behind: a Purchase Order Item pointing at the request
		frappe.db.sql(
			"""insert into `tabPurchase Order Item` (name, parent, parenttype, parentfield, material_request, docstatus, item_code, qty, creation, modified, owner, modified_by, idx)
			values ('zz-po-row', 'PO-FAKE', 'Purchase Order', 'items', %s, 1, %s, 6, now(), now(), 'Administrator', 'Administrator', 1)""",
			(mr.name, mr.items[0].item_code),
		)
		try:
			changed = make_request(
				"approved", "2026-10-28T08:00:00Z", lines=[line(LINE_1, CHEESE, "8.000", "7.000")]
			)
			log = self.send(changed, "purchase_request.updated")
			self.assertEqual((log.status, log.error_class), ("Failed", "Fatal"))
			self.assertIn("already ordered", log.message)
			self.assertEqual(frappe.db.get_value("Material Request", mr.name, "docstatus"), 1)
			cancelled = make_request("cancelled", "2026-10-29T08:00:00Z", cancel_reason="No longer needed")
			log = self.send(cancelled, "purchase_request.cancelled")
			self.assertEqual(log.error_class, "Fatal")
			self.assertEqual(frappe.db.get_value("Material Request", mr.name, "docstatus"), 1)
		finally:
			frappe.db.sql("delete from `tabPurchase Order Item` where name='zz-po-row'")

	def test_rejected_and_cancelled_cancel_the_material_request(self):
		self.send(make_request())
		cancelled = make_request("cancelled", "2026-10-27T09:00:00Z", cancel_reason="Not needed")
		log = self.send(cancelled, "purchase_request.cancelled")
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertEqual(frappe.get_doc("Material Request", log.reference_name).docstatus, 2)
		self.assertEqual(self.send(cancelled, "purchase_request.cancelled", eid="again").status, "Done")

	def test_rejected_or_cancelled_without_a_material_request_is_skipped(self):
		rejected = make_request(
			"rejected",
			lines=[line(LINE_1, CHEESE, "8.000", None, status="rejected", reject_reason="Too much")],
			reject_reason="Too much",
		)
		log = self.send(rejected, "purchase_request.rejected")
		self.assertEqual(log.status, "Skipped")

	def test_an_older_copy_arriving_late_changes_nothing(self):
		self.send(make_request("approved", "2026-10-26T12:00:00Z"))
		late = make_request(
			"approved", "2026-10-26T10:00:00Z", lines=[line(LINE_1, CHEESE, "8.000", "1.000")]
		)
		self.assertEqual(self.send(late, "purchase_request.updated").status, "Done")
		self.assertEqual(flt(self.mr().items[0].qty, 3), 6)

	def test_a_branch_without_a_mapping_names_the_row(self):
		card = make_request(branch_id="unmapped-branch")
		log = self.send(card)
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn("unmapped-branch", log.message)

	def test_off_by_default(self):
		self.conn_a.reload()
		self.conn_a.sync_purchase_requests = 0
		self.conn_a.save()
		log = self.send(make_request())
		self.assertEqual(log.status, "Skipped")
		self.assertIn("Sync Purchase Requests", log.message)

	def test_koomyos_text_is_escaped(self):
		card = make_request(notes="<script>alert(1)</script>")
		self.send(card)
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Material Request", "reference_name": self.mr().name},
			pluck="content",
		)
		self.assertTrue(comments)
		self.assertFalse([c for c in comments if "<script>" in c])


class TestSafetyNet(RequestCase):
	def test_updated_since_runs_the_same_path_and_keeps_its_place(self):
		self.fake_a.lists["/purchase-requests"] = [
			make_request(
				"submitted",
				"2026-10-26T09:14:30Z",
				lines=[line(LINE_1, CHEESE, "8.000", None, status="submitted")],
				id="waiting-1",
			),
			make_request("approved", "2026-10-26T10:02:00Z"),
		]
		with self.koomyo():
			summary = purchase_requests.sync_purchase_requests(self.conn_a.name)
			events.process_pending(self.conn_a.name)
		self.assertEqual(summary, {"read": 2, "queued": 2})
		self.assertIsNotNone(self.mr())
		self.assertIsNone(self.mr("waiting-1"))
		self.conn_a.reload()
		self.assertEqual(self.conn_a.purchase_request_cursor, "2026-10-26T10:02:00Z")
		with self.koomyo():
			second = purchase_requests.sync_purchase_requests(self.conn_a.name)
			events.process_pending(self.conn_a.name)
		self.assertEqual(second["queued"], 0)
		self.assertEqual(
			len(frappe.get_all("Material Request", filters={"koomyo_connection": self.conn_a.name})), 1
		)

	def test_a_restaurant_without_the_feature_is_said_plainly(self):
		self.fake_a.purchase_requests_off = True
		with self.koomyo():
			summary = purchase_requests.sync_purchase_requests(self.conn_a.name)
		self.assertIn("Inventory app", summary["error"])
		self.assertIn("feature_not_available", summary["error"])

	def test_test_connection_says_so_when_the_feature_is_off_and_does_not_probe(self):
		self.fake_a.purchase_requests_off = True
		with self.koomyo():
			result = self.conn_a.test_connection()
		self.assertTrue(
			[w for w in result["warnings"] if "purchase requests" in w and "Inventory app" in w],
			result["warnings"],
		)
		self.assertFalse([c for c in self.fake_a.calls if c["path"] == "/purchase-requests"])
		self.assertEqual(
			frappe.db.get_value("Koomyo Connection", self.conn_a.name, "purchase_requests_state"),
			"disabled:-",
		)

	def test_test_connection_warns_when_koomyo_still_orders_itself(self):
		self.fake_a.handled_by = "koomyo"
		with self.koomyo():
			result = self.conn_a.test_connection()
		self.assertTrue([w for w in result["warnings"] if "purchasing still orders" in w], result["warnings"])
		self.assertEqual(result["purchase_requests"], {"enabled": True, "handled_by": "koomyo"})

	def test_no_warning_when_an_external_system_orders(self):
		with self.koomyo():
			result = self.conn_a.test_connection()
		self.assertFalse(
			[w for w in result["warnings"] if "purchase requests" in w.lower() or "purchasing still" in w]
		)
		self.assertEqual(
			frappe.db.get_value("Koomyo Connection", self.conn_a.name, "purchase_requests_state"),
			"enabled:external",
		)

	def test_an_older_koomyo_without_the_field_is_asked_for_one_request(self):
		self.fake_a.account_purchase_requests = False
		self.fake_a.purchase_requests_off = True
		with self.koomyo():
			result = self.conn_a.test_connection()
		self.assertTrue([w for w in result["warnings"] if "Inventory app" in w], result["warnings"])
		calls = [c for c in self.fake_a.calls if c["path"] == "/purchase-requests"]
		self.assertEqual(calls[0]["params"].get("limit"), 1)

	def test_the_read_checks_again_when_it_was_disabled_and_reads_once_enabled(self):
		self.fake_a.purchase_requests_off = True
		with self.koomyo():
			self.conn_a.test_connection()
			summary = purchase_requests.sync_purchase_requests(self.conn_a.name)
		self.assertIn("not enabled", summary["error"])
		self.assertFalse([c for c in self.fake_a.calls if c["path"] == "/purchase-requests"])
		self.fake_a.purchase_requests_off = False
		self.fake_a.lists["/purchase-requests"] = [make_request()]
		with self.koomyo():
			summary = purchase_requests.sync_purchase_requests(self.conn_a.name)
		self.assertEqual(summary["read"], 1)

	def test_test_connection_is_quiet_when_it_works_or_the_switch_is_off(self):
		self.fake_a.lists["/purchase-requests"] = []
		with self.koomyo():
			result = self.conn_a.test_connection()
		self.assertFalse([w for w in result["warnings"] if "purchase requests" in w])
		self.conn_a.reload()
		self.conn_a.sync_purchase_requests = 0
		self.conn_a.save()
		self.fake_a.calls.clear()
		with self.koomyo():
			self.conn_a.test_connection()
		self.assertFalse([c for c in self.fake_a.calls if c["path"] == "/purchase-requests"])
