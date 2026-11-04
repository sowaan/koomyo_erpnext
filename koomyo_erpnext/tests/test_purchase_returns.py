"""Item 4: goods sent back to the supplier (an ERPNext Purchase Return) go to Koomyo as POST /purchase-orders/{id}/returns.

A line cannot go back for more than arrived (409 return_exceeds_received); an order received at several places
needs the place (409 destination_required); a reason code is required.
"""

import frappe

from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry
from koomyo_erpnext.tests.test_purchasing import PurchasingTestCase


class TestPurchaseReturns(PurchasingTestCase):
	def setUp(self):
		super().setUp()
		self.po = self.make_po(qty=10)
		self.run_queue()
		self.receipt = self.receive(self.po, 10)
		self.run_queue()
		self.fake_a.writes.clear()

	def tearDown(self):
		for name in frappe.get_all(
			"Purchase Receipt", filters={"company": self.conn_a.company, "is_return": 1}, pluck="name"
		):
			for table in ("GL Entry", "Stock Ledger Entry"):
				frappe.db.delete(table, {"voucher_no": name})
			frappe.db.delete("Purchase Receipt Item", {"parent": name})
			frappe.db.delete("Purchase Receipt", {"name": name})
		frappe.db.commit()
		super().tearDown()

	def give_back(self, qty, reason=None, submit=True):
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_return

		ret = make_purchase_return(self.receipt.name)
		for row in ret.items:
			row.qty = row.received_qty = -qty
		if reason:
			ret.koomyo_return_reason = reason
		ret.insert()
		if submit:
			ret.submit()
		frappe.db.commit()
		self.run_queue()
		return ret

	def logs_of_kind(self):
		return [l for l in self.outbound() if l.event_type == "erpnext.purchase_return"]

	def test_a_return_goes_back_with_the_line_quantity_and_reason(self):
		ret = self.give_back(3, reason="Damaged")
		(log,) = self.logs_of_kind()
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		(call,) = self.fake_a.return_calls
		self.assertEqual(call["reason_code"], "damaged")
		self.assertEqual(call["lines"], [{"line_id": "po-1-line-0", "quantity": "3"}])
		self.assertEqual(call["note"], f"ERPNext return {ret.name}")
		self.assertEqual(call["updated_at"], "2026-10-12T11:01:00Z")
		order = self.fake_a.purchase_orders["po-1"]
		self.assertEqual(order["lines"][0]["returned_quantity"], "3")
		self.assertEqual(frappe.db.get_value("Purchase Receipt", ret.name, "koomyo_id"), "po-1-ret-1")
		self.assertEqual(self.fake_a.receive_calls and len(self.writes("/purchase-orders/po-1/receive")), 0)

	def test_a_return_is_never_sent_as_a_delivery(self):
		self.give_back(2)
		self.assertEqual(self.writes("/purchase-orders/po-1/receive"), [])
		self.assertEqual([l.event_type for l in self.outbound()].count("erpnext.purchase_receipt"), 1)

	def test_without_a_reason_it_is_other(self):
		self.give_back(1)
		self.assertEqual(self.fake_a.return_calls[0]["reason_code"], "other")

	def test_more_than_arrived_is_refused_with_what_to_do(self):
		# staff already sent 8 back in Koomyo's admin, so 5 more is more than arrived (ERPNext cannot know)
		self.fake_a.purchase_orders["po-1"]["lines"][0]["returned_quantity"] = "8"
		self.give_back(5)
		(log,) = self.logs_of_kind()
		self.assertEqual(log.status, "Failed")
		self.assertIn("cannot go back for more than arrived", log.message)
		self.assertEqual(self.fake_a.return_calls, [])

	def test_the_place_is_asked_for_by_koomyo_and_the_message_says_so(self):
		self.fake_a.destination_required = True
		self.give_back(1)
		(log,) = self.logs_of_kind()
		self.assertEqual(log.status, "Failed")
		self.assertIn("received at several places", log.message)

	def test_a_stale_order_is_read_again_once(self):
		self.fake_a.stale_once = True
		self.give_back(1)
		(log,) = self.logs_of_kind()
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertEqual(len(self.fake_a.return_calls), 1)

	def test_a_retry_after_a_refusal_sends_once(self):
		self.fake_a.destination_required = True
		self.give_back(1)
		(log,) = self.logs_of_kind()
		self.fake_a.destination_required = False
		retry([log.name])
		self.run_queue()
		self.assertEqual(len(self.fake_a.return_calls), 1)
		retry([log.name])  # replaying a Done row changes nothing
		self.run_queue()
		self.assertEqual(len(self.fake_a.return_calls), 1)

	def test_below_zero_is_a_warning_on_a_done_row(self):
		self.fake_a.below_zero = True
		self.give_back(1)
		(log,) = self.logs_of_kind()
		self.assertEqual(log.status, "Done")
		self.assertIn("below zero", log.message)

	def test_a_delivery_made_by_koomyo_is_not_sent(self):
		frappe.db.set_value("Purchase Receipt", self.receipt.name, "koomyo_sent", 0)
		frappe.db.commit()
		self.give_back(1)
		self.assertEqual(self.logs_of_kind(), [])

	def test_a_return_cannot_be_cancelled_in_erpnext_because_koomyo_cannot_undo_it(self):
		ret = self.give_back(1)
		self.fake_a.writes.clear()
		with self.assertRaises(frappe.ValidationError) as caught:
			frappe.get_doc("Purchase Receipt", ret.name).cancel()
		self.assertIn("no call to undo a return", str(caught.exception))
		self.assertEqual(frappe.db.get_value("Purchase Receipt", ret.name, "docstatus"), 1)
		self.assertEqual(self.fake_a.writes, [])
