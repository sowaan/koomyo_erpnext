"""Item 3: a Purchase Invoice that charges another price than its Purchase Receipt restates that delivery's cost.

POST /purchase-orders/{id}/receipts/{receipt_id}/restate-costs changes only the unit cost of the listed
lines of that delivery; Koomyo then applies the restaurant's ingredient cost rule (`cost_rule`).
"""

import frappe

from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry
from koomyo_erpnext.tests.test_purchase_requests import MOZZARELLA
from koomyo_erpnext.tests.test_purchasing import PurchasingTestCase


class TestCostRestate(PurchasingTestCase):
	def setUp(self):
		super().setUp()
		# ERPNext holds the invoice rate to the order's rate unless this is off: a price difference needs it off
		self.same_rate = frappe.db.get_single_value("Buying Settings", "maintain_same_rate")
		frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
		self.over_billing = frappe.db.get_single_value("Accounts Settings", "over_billing_allowance")
		frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", 100)
		self.conn_a.reload()
		self.conn_a.restate_costs_from_invoices = 1
		self.conn_a.save()
		self.po = self.make_po(qty=10, rate=1450)
		self.run_queue()
		self.receipt = self.receive(self.po, 10)
		self.run_queue()
		self.fake_a.writes.clear()

	def tearDown(self):
		frappe.db.set_single_value("Buying Settings", "maintain_same_rate", self.same_rate)
		frappe.db.set_single_value("Accounts Settings", "over_billing_allowance", self.over_billing)
		for name in frappe.get_all(
			"Purchase Invoice", filters={"company": self.conn_a.company}, pluck="name"
		):
			for table in ("GL Entry", "Stock Ledger Entry", "Payment Ledger Entry"):
				frappe.db.delete(table, {"voucher_no": name})
			frappe.db.delete("Purchase Invoice Item", {"parent": name})
			frappe.db.delete("Purchase Taxes and Charges", {"parent": name})
			frappe.db.delete("Purchase Invoice", {"name": name})
		frappe.db.commit()
		super().tearDown()

	def invoice(self, rate, bill_no="INV-20418"):
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice

		pi = make_purchase_invoice(self.receipt.name)
		pi.bill_no = bill_no
		pi.posting_date = "2026-10-15"
		for row in pi.items:
			row.rate = rate
		pi.insert()
		pi.submit()
		frappe.db.commit()
		self.run_queue()
		return pi

	def logs_of_kind(self):
		return [l for l in self.outbound() if l.event_type == "erpnext.purchase_invoice_restate"]

	def test_a_different_price_restates_that_deliverys_unit_cost(self):
		pi = self.invoice(1480)
		(log,) = self.logs_of_kind()
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		(call,) = self.fake_a.restate_calls
		self.assertEqual(call["receipt_id"], "po-1-rcpt-1")
		self.assertEqual(call["reason"], "Supplier invoice INV-20418")
		self.assertEqual(call["lines"], [{"line_id": "po-1-line-0", "unit_cost": "1480"}])
		self.assertEqual(call["updated_at"], "2026-10-12T11:01:00Z")
		self.assertEqual(log.reference_name, pi.name)
		self.assertEqual(self.fake_a.purchase_orders["po-1"]["receipts"][0]["costs"], {"po-1-line-0": "1480"})

	def test_the_cost_rule_ask_is_said_on_the_row(self):
		self.fake_a.cost_rule = "ask"
		self.invoice(1480)
		(log,) = self.logs_of_kind()
		self.assertEqual(log.status, "Done")
		self.assertIn("cost rule is 'ask'", log.message)

	def test_the_same_price_sends_nothing(self):
		self.invoice(1450)
		self.assertEqual(self.logs_of_kind(), [])
		self.assertEqual(self.fake_a.restate_calls, [])

	def test_switched_off_by_default_sends_nothing(self):
		self.conn_a.reload()
		self.conn_a.restate_costs_from_invoices = 0
		self.conn_a.save()
		self.invoice(1480)
		self.assertEqual(self.logs_of_kind(), [])

	def test_a_reversed_delivery_cannot_be_restated(self):
		self.fake_a.purchase_orders["po-1"]["receipts"][0]["reversed_at"] = "2026-10-14T00:00:00Z"
		self.invoice(1480)
		(log,) = self.logs_of_kind()
		self.assertEqual(log.status, "Failed")
		self.assertIn("reversed in Koomyo", log.message)

	def test_a_stale_order_is_read_again_once(self):
		self.fake_a.stale_once = True
		self.invoice(1480)
		(log,) = self.logs_of_kind()
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))

	def test_a_retry_after_a_refusal_works(self):
		self.fake_a.purchase_orders["po-1"]["receipts"][0]["reversed_at"] = "2026-10-14T00:00:00Z"
		self.invoice(1480)
		(log,) = self.logs_of_kind()
		self.fake_a.purchase_orders["po-1"]["receipts"][0].pop("reversed_at")
		retry([log.name])
		self.run_queue()
		self.assertEqual(len(self.fake_a.restate_calls), 1)

	def test_a_receipt_made_from_a_koomyo_delivery_is_left_alone(self):
		frappe.db.set_value("Purchase Receipt", self.receipt.name, "koomyo_sent", 0)
		frappe.db.commit()
		self.invoice(1480)
		self.assertEqual(self.logs_of_kind(), [])
