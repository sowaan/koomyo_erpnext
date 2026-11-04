"""A Purchase Order deleted in ERPNext, a number reused, and the identity of a document.

ERPNext reuses a Purchase Order number after a delete. Koomyo is cleaned up first (cancel, reverse, unlink), a
Koomyo order made before the ERPNext document existed is never taken for it, and a push is refused rather than
linked to the old order.
"""

import copy

import frappe

from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry
from koomyo_erpnext.tests.test_purchase_orders import event_for, make_order
from koomyo_erpnext.tests.test_purchasing import PurchasingTestCase


class TestDeleteAndReuse(PurchasingTestCase):
	def cleanup_logs(self):
		return [l for l in self.outbound() if l.event_type == "erpnext.purchase_order_cancel"]

	def test_deleting_a_cancelled_order_cleans_up_once_and_a_second_run_changes_nothing(self):
		po = self.make_po()
		self.run_queue()
		frappe.get_doc("Purchase Order", po.name).cancel()
		frappe.db.commit()
		self.run_queue()
		self.assertEqual(self.fake_a.purchase_orders["po-1"]["status"], "cancelled")
		frappe.delete_doc("Purchase Order", po.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		self.run_queue()
		logs = self.cleanup_logs()
		self.assertEqual(sorted(l.status for l in logs), ["Done", "Done"], [l.message for l in logs])
		self.assertEqual(len(self.writes("/purchase-orders/po-1/cancel")), 1, "cancelled once")

	def test_deleting_an_order_whose_cleanup_failed_tries_again(self):
		from koomyo_erpnext.tests.fake_koomyo import error_response

		po = self.make_po()
		self.run_queue()
		self.fake_a.scripted["/purchase-orders/po-1/cancel"] = [
			error_response(409, "partially_received", "x")
		]
		frappe.get_doc("Purchase Order", po.name).cancel()
		frappe.db.commit()
		self.run_queue()
		self.assertEqual([l.status for l in self.cleanup_logs()], ["Failed"])
		frappe.delete_doc("Purchase Order", po.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		self.run_queue()
		self.assertEqual(self.fake_a.purchase_orders["po-1"]["status"], "cancelled")
		self.assertEqual(self.fake_a.deleted_refs, [("purchase_order", "po-1")])

	def test_deleting_a_draft_that_was_never_sent_says_nothing(self):
		po = self.make_po(submit=False)
		frappe.delete_doc("Purchase Order", po.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		self.run_queue()
		self.assertEqual(self.cleanup_logs(), [])

	def stale_order(self, po):
		"""The old Koomyo order still linked to this number: made before the ERPNext document existed."""
		old = {
			"id": "old-1",
			"object": "purchase_order",
			"status": "received",
			"_ref": ("erpnext", po.name),
			"external_refs": {"erpnext": po.name},
			"supplier": {"id": "s", "name": "S"},
			"number": "PO-OLD-0004",
			"created_at": "2026-10-01T09:00:00Z",
			"updated_at": "2026-10-01T10:00:00Z",
			"receipts": [],
			"lines": [],
			"total_ordered": "900.00",
		}
		self.fake_a.purchase_orders["old-1"] = old
		return old

	def test_a_number_reused_after_a_delete_is_not_linked_to_the_old_order(self):
		po = self.make_po(qty=10)  # queued but not yet pushed
		self.stale_order(po)
		self.run_queue()
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_order"]
		self.assertEqual((log.status, log.error_class), ("Failed", "Fatal"))
		self.assertIn("reused the number", log.message)
		self.assertIn("PO-OLD-0004", log.message)
		self.assertFalse(frappe.db.get_value("Purchase Order", po.name, "koomyo_id"), "nothing was linked")
		self.assertEqual(self.fake_a.mark_calls, [], "and nothing was marked or changed in Koomyo")
		self.assertEqual(self.fake_a.purchase_orders["old-1"]["status"], "received")

	def test_after_the_old_order_is_cleared_a_retry_makes_the_new_order(self):
		po = self.make_po(qty=10)
		old = self.stale_order(po)
		self.run_queue()
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_order"]
		old["external_refs"], old["_ref"] = {}, None  # cancelled and unlinked in Koomyo
		retry([log.name])
		self.run_queue()
		self.assertEqual(frappe.db.get_value("Koomyo Event Log", log.name, "status"), "Done")
		self.assertEqual(frappe.db.get_value("Purchase Order", po.name, "koomyo_id"), "po-2")

	def test_the_key_names_the_body_so_another_body_is_another_key(self):
		po = self.make_po(qty=10)
		self.run_queue()
		(call,) = [w for w in self.writes("/purchase-orders") if w["path"] == "/purchase-orders"]
		self.assertRegex(call["key"], r"po-[0-9a-f]{10}")
		self.assertIn(po.name, call["key"])

	def test_an_event_for_the_old_order_does_not_touch_the_new_document(self):
		po = self.make_po(qty=10)
		self.run_queue()
		frappe.db.set_value("Purchase Order", po.name, {"koomyo_connection": None, "koomyo_id": None})
		frappe.db.commit()
		old = copy.deepcopy(make_order("received", "2026-10-13T09:00:00Z", id="old-9"))
		old["created_at"] = "2026-10-01T09:00:00Z"
		old["external_refs"] = {"erpnext": po.name}
		log = self.book(event_for(old, "purchase.updated", 970))
		self.assertEqual((log.status, log.error_class), ("Failed", "Fatal"))
		self.assertIn("ERPNext reused the number", log.message)
		self.assertEqual(frappe.get_all("Purchase Receipt", filters={"company": self.conn_a.company}), [])
