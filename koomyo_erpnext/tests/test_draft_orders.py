"""Keep Koomyo Draft Orders as Drafts: a draft Purchase Order that follows Koomyo and is deleted when Koomyo drops it."""

import frappe

from koomyo_erpnext.sync import events, purchase_orders
from koomyo_erpnext.tests.test_purchase_orders import ORDER_ID, PurchaseOrderCase, make_order


class DraftCase(PurchaseOrderCase):
	def setUp(self):
		super().setUp()
		self.conn_a.reload()
		self.conn_a.keep_draft_orders = 1
		self.conn_a.save()

	def draft(self, **changes):
		return make_order("draft", "2026-10-12T17:26:12Z", **changes)


class TestDraftOrders(DraftCase):
	def test_a_draft_becomes_a_draft_purchase_order_that_moves_nothing(self):
		before = self.bin_total()
		log = self.send(self.draft(), "purchase.created")
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		po = self.po()
		self.assertEqual(po.docstatus, 0)
		notes = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Purchase Order", "reference_name": po.name},
			pluck="content",
		)
		self.assertTrue([n for n in notes if "Draft in Koomyo" in n])
		self.assertEqual(self.bin_total(), before)
		self.assertEqual(frappe.get_all("GL Entry", filters={"voucher_no": po.name}), [])

	def bin_total(self):
		return frappe.db.sql("select coalesce(sum(actual_qty + ordered_qty), 0) from tabBin")[0][0]

	def test_a_second_copy_changes_nothing_and_an_edit_rebuilds_the_same_draft(self):
		self.send(self.draft(), "purchase.created")
		name = self.po().name
		self.send(self.draft(), "purchase.updated")
		self.assertEqual(self.po().name, name)
		edited = make_order("draft", "2026-10-12T18:00:00Z")
		edited["lines"][0].update({"quantity_ordered": "12.000", "line_total": "17400.00"})
		edited["total_ordered"] = "17400.00"
		edited["items_without_tax"] = "17400.00"
		log = self.send(edited, "purchase.updated")
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		po = self.po()
		self.assertEqual((po.name, po.docstatus, po.items[0].qty), (name, 0, 12))
		self.assertEqual(len(frappe.get_all("Purchase Order", filters={"koomyo_id": ORDER_ID})), 1)

	def test_ordering_it_in_koomyo_submits_the_same_document(self):
		self.send(self.draft(), "purchase.created")
		name = self.po().name
		log = self.send(make_order("ordered", "2026-10-12T19:00:00Z"), "purchase.updated")
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		po = self.po()
		self.assertEqual((po.name, po.docstatus), (name, 1))

	def test_cancelled_in_koomyo_deletes_the_draft(self):
		self.send(self.draft(), "purchase.created")
		name = self.po().name
		log = self.send(make_order("cancelled", "2026-10-12T19:00:00Z"), "purchase.cancelled")
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertIn("was deleted", log.message)
		self.assertFalse(frappe.db.exists("Purchase Order", name))

	def test_deleted_in_koomyo_is_found_by_the_safety_net_and_deleted(self):
		self.send(self.draft(), "purchase.created")
		name = self.po().name
		# Koomyo no longer has it: it is not in the lists and GET answers 404
		self.fake_a.lists["/purchase-orders"] = []
		with self.koomyo():
			summary = purchase_orders.sync_purchase_orders(self.conn_a.name)
			events.process_pending(self.conn_a.name)
		self.assertEqual(summary["queued"], 1)
		self.assertFalse(frappe.db.exists("Purchase Order", name))
		(log,) = [l for l in self.logs(self.conn_a.name) if l.event_type == purchase_orders.DRAFT_GONE]
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))

	def test_a_draft_that_still_exists_is_left_alone(self):
		self.send(self.draft(), "purchase.created")
		self.fake_a.purchase_orders[ORDER_ID] = self.draft()
		self.fake_a.lists["/purchase-orders"] = []
		with self.koomyo():
			summary = purchase_orders.sync_purchase_orders(self.conn_a.name)
		self.assertEqual(summary["queued"], 0)
		self.assertTrue(self.po())

	def test_it_cannot_be_submitted_by_hand(self):
		self.send(self.draft(), "purchase.created")
		po = self.po()
		with self.assertRaises(frappe.ValidationError):
			po.submit()
		self.assertEqual(self.po().docstatus, 0)

	def test_nothing_is_sent_to_koomyo_for_a_draft(self):
		self.send(self.draft(), "purchase.created")
		self.assertEqual(self.fake_a.writes, [])

	def test_off_by_default_still_skips(self):
		self.conn_a.reload()
		self.conn_a.keep_draft_orders = 0
		self.conn_a.save()
		log = self.send(self.draft(), "purchase.created")
		self.assertEqual(log.status, "Skipped")
		self.assertIsNone(self.po())

	def test_it_needs_book_orders_made_in_koomyo(self):
		self.conn_a.reload()
		self.conn_a.book_koomyo_purchases = 0
		self.conn_a.save()
		log = self.send(self.draft(), "purchase.created")
		self.assertEqual(log.status, "Skipped")
		self.assertIsNone(self.po())
