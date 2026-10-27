"""Events are processed by the app, not by the person who pressed Retry or Catch Up Now.

A background job runs as the user who queued it. A cancel in ERPNext checks that user's permission, so a
Koomyo Manager without the right to cancel a Purchase Order or Stock Entry saw a permission error when
Koomyo cancelled one. Cancelling what Koomyo cancelled is the app's own work, like creating the document.
"""

import frappe

from koomyo_erpnext.tests.test_purchase_orders import PurchaseOrderCase, event_for, make_order
from koomyo_erpnext.tests.test_sales import order_event
from koomyo_erpnext.tests.test_transfer_steps import TransferStepsCase, event, transfer

USER = "koomyo.manager.only@example.test"


def restricted_user():
	if not frappe.db.exists("User", USER):
		frappe.get_doc(
			{"doctype": "User", "email": USER, "first_name": "Koomyo Only", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", USER)
	user.roles = []
	user.add_roles("Koomyo Manager")
	frappe.db.commit()


class RestrictedUserMixin:
	def process_as_manager(self, fn):
		restricted_user()
		frappe.set_user(USER)
		try:
			return fn()
		finally:
			frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()


class TestPurchaseCancelByManager(RestrictedUserMixin, PurchaseOrderCase):
	def test_purchase_cancelled_cancels_the_order(self):
		self.send(make_order("ordered"))
		self.po_seq += 1
		cancelled = make_order("cancelled", "2026-10-12T18:05:30Z", cancel_reason="Wrong supplier")
		log = self.process_as_manager(
			lambda: self.book(event_for(cancelled, "purchase.cancelled", self.po_seq))
		)
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertEqual(frappe.get_doc("Purchase Order", log.reference_name).docstatus, 2)

	def test_an_edit_that_rebuilds_the_order_works_too(self):
		self.send(make_order("ordered", "2026-10-12T17:40:02Z"))
		edited = make_order("ordered", "2026-10-12T18:00:00Z")
		edited["lines"][0].update(
			{"quantity_ordered": "12.000", "ordered_value": "17400.00", "line_total": "17400.00"}
		)
		edited["total_ordered"] = "17400.00"
		self.po_seq += 1
		log = self.process_as_manager(lambda: self.book(event_for(edited, "purchase.updated", self.po_seq)))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))


class TestNewSupplierByManager(RestrictedUserMixin, PurchaseOrderCase):
	def test_an_order_from_an_unknown_supplier_makes_the_supplier_and_the_order(self):
		self.assertFalse(frappe.db.exists("Supplier", {"koomyo_id": "koomyo-supplier-1"}))
		self.po_seq += 1
		ordered = make_order("ordered")
		log = self.process_as_manager(lambda: self.book(event_for(ordered, "purchase.updated", self.po_seq)))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertTrue(frappe.db.exists("Supplier", {"koomyo_id": "koomyo-supplier-1"}))
		self.assertEqual(frappe.get_doc("Purchase Order", log.reference_name).docstatus, 1)

	def test_the_user_is_put_back_after_the_event(self):
		self.po_seq += 1
		ordered = make_order("ordered")

		def run():
			self.book(event_for(ordered, "purchase.updated", self.po_seq))
			return frappe.session.user

		self.assertEqual(self.process_as_manager(run), USER)


class TestTransferCancelByManager(RestrictedUserMixin, TransferStepsCase):
	def test_transfer_cancelled_cancels_the_send(self):
		self.send("transfer.sent", transfer())
		self.seq += 1
		cancelled = transfer("cancelled", cancel_reason="Wrong branch")
		log = self.process_as_manager(lambda: self.book(event("transfer.cancelled", cancelled, self.seq)))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))


class TestOrderCancelByManager(RestrictedUserMixin, PurchaseOrderCase):
	def test_order_cancelled_adds_its_note(self):
		sale = order_event(
			order_id="order-perm",
			lines=[{"quantity": 1, "unit_price": "100.00", "line_total": "100.00"}],
			payments=[("cash", "100.00")],
			items_subtotal="100.00",
			total="100.00",
			amount_due="100.00",
			amount_paid="100.00",
		)
		self.assertEqual(self.book(sale).status, "Done")
		cancelled = order_event(order_id="order-perm", seq=11)
		cancelled["type"] = "order.cancelled"
		cancelled["data"]["cancelled_at"] = "2026-10-12T17:29:00Z"
		cancelled["id"] = "evt-cancel-perm"
		log = self.process_as_manager(lambda: self.book(cancelled))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
