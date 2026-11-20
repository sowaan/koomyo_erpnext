"""Koomyo Number: Koomyo's own readable number next to the unique id, on the documents the app makes."""

import frappe

from koomyo_erpnext.patches import fill_koomyo_number
from koomyo_erpnext.tests import test_purchase_orders as po_tests
from koomyo_erpnext.tests import test_purchase_requests as pr_tests
from koomyo_erpnext.tests import test_transfer_steps as ts_tests
from koomyo_erpnext.tests.fake_koomyo import load_fixture
from koomyo_erpnext.tests.test_sales import SalesTestCase, refund_event

TOP_OF_FORM = {  # next to the customer or supplier, not buried in the collapsed Koomyo section
	"Sales Invoice": "customer_name",
	"Payment Entry": "party_name",
	"Purchase Order": "supplier_name",
	"Purchase Receipt": "supplier_name",
	"Material Request": "material_request_type",
	"Stock Entry": "stock_entry_type",
}
ORDER_ID = "a8955e7a-335a-4ad0-a872-56b6d2479f80"
ORDER_NUMBER = "ORD-PFB-20261012-0002"


class TestKoomyoNumber(SalesTestCase):
	def sale(self):
		self.assertEqual(
			self.book(
				load_fixture("event_order_paid") | {"tenant_id": "bd0c698a-158d-47a0-9a8c-f4e899f2569e"}
			).status,
			"Done",
		)
		return self.invoice(ORDER_ID)

	def test_the_invoice_its_payment_and_the_credit_note_carry_the_orders_number(self):
		inv = self.sale()
		self.assertEqual(inv.koomyo_number, ORDER_NUMBER)
		(pe,) = self.payments(inv)
		self.assertEqual(pe.koomyo_number, ORDER_NUMBER)
		log = self.book(refund_event(order_id=ORDER_ID, amount="724.50"))
		self.assertEqual(log.status, "Done", log.message)
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", log.reference_name, "koomyo_number"), ORDER_NUMBER
		)

	def test_the_field_is_visible_searchable_read_only_and_at_the_top_of_the_form(self):
		for doctype in fill_koomyo_number.DOCTYPES:
			field = frappe.get_meta(doctype).get_field("koomyo_number")
			self.assertIsNotNone(field, doctype)
			self.assertEqual(
				(
					field.read_only,
					field.hidden,
					field.in_list_view,
					field.in_standard_filter,
					field.insert_after,
				),
				(1, 0, 1, 1, TOP_OF_FORM[doctype]),
				doctype,
			)
			self.assertEqual(frappe.get_meta(doctype).get_field("koomyo_id").hidden, 0)

	def test_a_number_that_is_not_text_is_ignored_and_a_long_one_is_cut(self):
		from koomyo_erpnext.sync.common import stamp

		doc = frappe.new_doc("Sales Invoice")
		stamp(doc, self.conn_a, "id-1", number={"x": 1})
		self.assertFalse(doc.get("koomyo_number"))
		stamp(doc, self.conn_a, "id-1", number="N" * 300)
		self.assertEqual(len(doc.koomyo_number), 140)
		stamp(frappe.new_doc("Customer"), self.conn_a, "id-2", number="ignored: no such field")  # no error

	def test_the_patch_fills_old_documents_from_the_event_log_and_never_overwrites(self):
		inv = self.sale()
		(pe,) = self.payments(inv)
		frappe.db.set_value("Sales Invoice", inv.name, "koomyo_number", None)
		frappe.db.set_value("Payment Entry", pe.name, "koomyo_number", None)
		frappe.db.commit()
		fill_koomyo_number.execute()
		self.assertEqual(frappe.db.get_value("Sales Invoice", inv.name, "koomyo_number"), ORDER_NUMBER)
		self.assertEqual(frappe.db.get_value("Payment Entry", pe.name, "koomyo_number"), ORDER_NUMBER)
		frappe.db.set_value("Sales Invoice", inv.name, "koomyo_number", "KEEP-ME")
		fill_koomyo_number.execute()
		self.assertEqual(frappe.db.get_value("Sales Invoice", inv.name, "koomyo_number"), "KEEP-ME")

	def test_the_patch_brings_the_custom_fields_in_before_it_fills_them(self):
		"""On migrate the patch runs before the fixtures, so it must import them itself (seen on the first run)."""
		from unittest.mock import patch

		with patch("frappe.utils.fixtures.sync_fixtures") as sync:
			fill_koomyo_number.execute()
		sync.assert_called_once_with(app="koomyo_erpnext")

	def test_the_patch_falls_back_to_the_invoices_own_order_number_when_the_log_is_gone(self):
		inv = self.sale()
		frappe.db.set_value("Sales Invoice", inv.name, "koomyo_number", None)
		frappe.db.delete(
			"Koomyo Event Log", {"reference_doctype": "Sales Invoice", "reference_name": inv.name}
		)
		fill_koomyo_number.execute()
		self.assertEqual(frappe.db.get_value("Sales Invoice", inv.name, "koomyo_number"), ORDER_NUMBER)


class TestNumberOnPurchasing(po_tests.PurchaseOrderCase):
	def test_the_order_and_its_delivery_carry_the_purchase_order_number(self):
		order = po_tests.make_order("ordered")
		self.assertTrue(order["number"], "the docs example order has a number")
		self.assertEqual(self.send(order, "purchase.created").status, "Done")
		self.assertEqual(self.po().koomyo_number, order["number"])
		received = po_tests.make_order(
			"received",
			"2026-10-14T09:00:00Z",
			receipts=[po_tests.receipt("rcpt-n", "10.000", at="2026-10-14T09:00:00Z")],
			fully_received=True,
		)
		self.assertEqual(self.send(received, "purchase.received").status, "Done")
		pr = frappe.get_doc(
			"Purchase Receipt", frappe.db.get_value("Purchase Receipt", {"koomyo_id": "rcpt-n"})
		)
		self.assertEqual(pr.koomyo_number, order["number"])
		# an old receipt with no number takes it from its order (the log row points at the order, not the receipt)
		frappe.db.set_value("Purchase Receipt", pr.name, "koomyo_number", None)
		fill_koomyo_number.execute()
		self.assertEqual(frappe.db.get_value("Purchase Receipt", pr.name, "koomyo_number"), order["number"])


class TestNumberOnRequests(pr_tests.RequestCase):
	def test_the_material_request_carries_the_requests_number(self):
		card = pr_tests.make_request()
		self.assertTrue(card["number"])
		self.assertEqual(self.send(card).status, "Done")
		self.assertEqual(self.mr().koomyo_number, card["number"])


class TestNumberOnTransfers(ts_tests.TransferStepsCase):
	def test_the_stock_entries_of_a_transfer_carry_its_number(self):
		data = ts_tests.transfer()
		self.assertTrue(data["number"])
		self.assertEqual(self.send("transfer.sent", data).status, "Done")
		(send,) = self.entries()
		self.assertEqual(frappe.db.get_value("Stock Entry", send.name, "koomyo_number"), data["number"])
