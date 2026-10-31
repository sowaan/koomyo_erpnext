"""Item 2: a Purchase Receipt sent to Koomyo and then cancelled reverses that delivery there.

POST /purchase-orders/{id}/receipts/{receipt_id}/reverse: a reason is required, only the latest
un-reversed delivery can be reversed (not_latest_receipt), and not one whose goods moved on
(receipt_distributed) or partly went back to the supplier (receipt_returned).
"""

import frappe

from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry
from koomyo_erpnext.tests.test_purchasing import PurchasingTestCase


class TestReceiptReversal(PurchasingTestCase):
	def setUp(self):
		super().setUp()
		self.po = self.make_po(qty=10)
		self.run_queue()
		self.first = self.receive(self.po, 6)
		self.run_queue()
		self.order = self.fake_a.purchase_orders["po-1"]
		self.fake_a.writes.clear()

	def cancel(self, receipt):
		frappe.get_doc("Purchase Receipt", receipt.name).cancel()
		frappe.db.commit()
		self.run_queue()

	def logs_of_kind(self):
		return [l for l in self.outbound() if l.event_type == "erpnext.purchase_receipt_reverse"]

	def test_the_delivery_is_reversed_with_a_reason_and_the_order_goes_back(self):
		read_at = self.order["updated_at"]
		self.cancel(self.first)
		(log,) = self.logs_of_kind()
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		(call,) = self.fake_a.reverse_calls
		self.assertEqual(call["receipt_id"], "po-1-rcpt-1")
		self.assertEqual(call["reason"], f"Cancelled in ERPNext ({self.first.name})")
		self.assertEqual(call["updated_at"], read_at)
		self.assertEqual(
			(self.order["status"], self.order["lines"][0]["quantity_received"]), ("ordered", "0")
		)
		self.assertTrue(self.order["receipts"][0]["reversed_at"])

	def test_a_replay_changes_nothing(self):
		self.cancel(self.first)
		self.run_queue()
		self.assertEqual(len(self.fake_a.reverse_calls), 1)

	def test_a_newer_delivery_must_be_cancelled_first_and_then_it_works(self):
		second = self.receive(self.po, 4)
		self.run_queue()
		self.fake_a.writes.clear()
		self.cancel(self.first)
		(log,) = self.logs_of_kind()
		self.assertEqual(log.status, "Failed")
		self.assertIn("Cancel the newer receipt(s) first", log.message)
		self.assertIn(second.name, log.message)
		self.assertEqual(self.fake_a.reverse_calls, [])
		self.cancel(second)
		self.assertEqual([c["receipt_id"] for c in self.fake_a.reverse_calls], ["po-1-rcpt-2"])
		retry([log.name])
		self.run_queue()
		self.assertEqual([c["receipt_id"] for c in self.fake_a.reverse_calls], ["po-1-rcpt-2", "po-1-rcpt-1"])
		self.assertEqual(self.order["status"], "ordered")

	def test_goods_that_went_on_by_transfer(self):
		self.fake_a.distributed.add("po-1-rcpt-1")
		self.cancel(self.first)
		(log,) = self.logs_of_kind()
		self.assertEqual(log.status, "Failed")
		self.assertIn("went on to branches by a transfer", log.message)

	def test_goods_that_went_back_to_the_supplier(self):
		self.fake_a.returned.add("po-1-rcpt-1")
		self.cancel(self.first)
		(log,) = self.logs_of_kind()
		self.assertEqual(log.status, "Failed")
		self.assertIn("sent back to the supplier", log.message)

	def test_already_reversed_is_said_plainly_and_is_not_a_failure(self):
		self.order["receipts"][0]["reversed_at"] = "2026-10-12T12:00:00Z"
		self.cancel(self.first)
		(log,) = self.logs_of_kind()
		self.assertEqual(log.status, "Skipped")
		self.assertIn("already reversed", log.message)
		self.assertEqual(self.fake_a.reverse_calls, [])

	def test_stock_already_used_is_a_warning_on_a_done_row(self):
		self.fake_a.below_zero = True
		self.cancel(self.first)
		(log,) = self.logs_of_kind()
		self.assertEqual(log.status, "Done")
		self.assertIn("below zero", log.message)

	def test_a_receipt_made_from_a_koomyo_delivery_is_left_to_koomyo(self):
		frappe.db.set_value("Purchase Receipt", self.first.name, "koomyo_sent", 0)
		frappe.db.commit()
		self.cancel(self.first)
		self.assertEqual(self.logs_of_kind(), [])
		self.assertEqual(self.fake_a.reverse_calls, [])
