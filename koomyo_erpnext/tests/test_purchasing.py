"""Slice 5: ERPNext purchasing written to Koomyo, and purchase.received back.

Writes go to the fake Koomyo, which keeps every call (method, path, Idempotency-Key, body).
"""

import copy

import frappe
from frappe.utils import flt

from koomyo_erpnext.sync import events
from koomyo_erpnext.tests.fake_koomyo import error_response, load_fixture
from koomyo_erpnext.tests.helpers import COMPANY_A, TENANT_A
from koomyo_erpnext.tests.test_stock import MOZZARELLA, StockTestCase, adjustment

BRANCH = "90000000-0000-4000-8000-000000000001"


class PurchasingTestCase(StockTestCase):
	def setUp(self):
		super().setUp()
		self.conn_a.reload()
		self.conn_a.sync_purchasing = 1
		self.conn_a.save()
		self.assertEqual(
			self.book(adjustment(1, "1.0000", ingredient=MOZZARELLA, unit_cost="1450.0000")).status, "Done"
		)
		self.cheese = self.item(MOZZARELLA)
		if not frappe.db.exists("Supplier Group", "_Koomyo Suppliers"):
			frappe.get_doc(
				{
					"doctype": "Supplier Group",
					"supplier_group_name": "_Koomyo Suppliers",
					"parent_supplier_group": "All Supplier Groups",
				}
			).insert()
		if not frappe.db.exists("Supplier", "_Koomyo Dairy"):
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "_Koomyo Dairy",
					"supplier_group": "_Koomyo Suppliers",
				}
			).insert()
		frappe.db.set_value("Supplier", "_Koomyo Dairy", {"koomyo_connection": None, "koomyo_id": None})
		frappe.db.commit()
		# Making the ingredient Item wrote its reference to Koomyo; count only what the tests send.
		self.assertEqual([w["path"] for w in self.fake_a.writes], [f"/refs/ingredient/{MOZZARELLA}"])
		self.fake_a.writes.clear()

	def tearDown(self):
		for doctype in ("Purchase Receipt", "Purchase Order"):
			for name in frappe.get_all(doctype, filters={"company": COMPANY_A}, pluck="name"):
				for table in (
					"GL Entry",
					"Stock Ledger Entry",
					"Payment Ledger Entry",
					"Repost Item Valuation",
				):
					frappe.db.delete(table, {"voucher_type": doctype, "voucher_no": name})
				from koomyo_erpnext.tests.helpers import hard_delete

				hard_delete(doctype, name)
		frappe.db.commit()
		super().tearDown()

	def make_po(self, warehouse="Stores - _KTA", qty=10, rate=1450, item=None, submit=True):
		po = frappe.get_doc(
			{
				"doctype": "Purchase Order",
				"company": COMPANY_A,
				"supplier": "_Koomyo Dairy",
				"transaction_date": "2026-10-12",
				"schedule_date": "2026-10-13",
				"set_warehouse": warehouse,
				"currency": "PKR",
				"conversion_rate": 1,
				"items": [
					{
						"item_code": item or self.cheese,
						"qty": qty,
						"rate": rate,
						"warehouse": warehouse,
						"schedule_date": "2026-10-13",
					}
				],
			}
		).insert()
		if submit:
			po.submit()
		frappe.db.commit()
		return po

	def receive(self, po, qty):
		from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

		pr = make_purchase_receipt(po.name)
		pr.posting_date = "2026-10-13"
		pr.set_posting_time = 1
		for row in pr.items:
			row.qty = row.received_qty = qty
		pr.insert()
		pr.submit()
		frappe.db.commit()
		return pr

	def run_queue(self):
		with self.koomyo():
			events.process_pending(self.conn_a.name)

	def outbound(self, **filters):
		return self.logs(self.conn_a.name, received_via="ERPNext", **filters)

	def writes(self, path_prefix):
		return [w for w in self.fake_a.writes if w["path"].startswith(path_prefix)]


class TestPurchaseOrders(PurchasingTestCase):
	def test_po_goes_to_koomyo_with_supplier(self):
		po = self.make_po()
		self.run_queue()
		(log,) = self.outbound()
		self.assertEqual((log.status, log.event_type), ("Done", "erpnext.purchase_order"), log.message)
		(supplier_call,) = self.writes("/suppliers")
		self.assertEqual(
			supplier_call["body"]["external_ref"], {"system": "erpnext", "external_id": "_Koomyo Dairy"}
		)
		(po_call,) = self.writes("/purchase-orders")
		self.assertEqual(po_call["body"]["branch_id"], BRANCH)
		self.assertEqual(
			po_call["body"]["lines"],
			[{"ingredient_id": MOZZARELLA, "quantity_ordered": "10", "unit_cost": "1450"}],
		)
		self.assertEqual(po_call["body"]["external_ref"]["external_id"], po.name)
		self.assertTrue(po_call["key"].startswith(f"erpnext-po-{po.name}-"))
		self.assertTrue(all(w["key"] for w in self.fake_a.writes), "every write has an Idempotency-Key")
		po.reload()
		self.assertEqual((po.koomyo_connection, po.koomyo_id), (self.conn_a.name, "po-1"))
		self.assertEqual(frappe.db.get_value("Supplier", "_Koomyo Dairy", "koomyo_id"), "sup-1")

	def test_uom_conversion_and_exact_costs(self):
		item = frappe.get_doc("Item", self.cheese)
		item.append("uoms", {"uom": "Gram", "conversion_factor": 0.001})
		item.save()
		po = frappe.get_doc(
			{
				"doctype": "Purchase Order",
				"company": COMPANY_A,
				"supplier": "_Koomyo Dairy",
				"transaction_date": "2026-10-12",
				"schedule_date": "2026-10-13",
				"set_warehouse": "Stores - _KTA",
				"currency": "PKR",
				"conversion_rate": 1,
				"items": [
					{
						"item_code": self.cheese,
						"qty": 2500,
						"uom": "Gram",
						"conversion_factor": 0.001,
						"rate": 1.45,
						"warehouse": "Stores - _KTA",
						"schedule_date": "2026-10-13",
					}
				],
			}
		).insert()
		po.submit()
		frappe.db.commit()
		self.run_queue()
		(po_call,) = self.writes("/purchase-orders")
		self.assertEqual(
			po_call["body"]["lines"][0],
			{"ingredient_id": MOZZARELLA, "quantity_ordered": "2.5", "unit_cost": "1450"},
		)

	def test_central_warehouse_order_has_no_branch(self):
		self.make_po(warehouse="Finished Goods - _KTA")
		self.run_queue()
		(po_call,) = self.writes("/purchase-orders")
		self.assertNotIn("branch_id", po_call["body"])

	def test_order_for_other_warehouse_is_not_sent(self):
		self.make_po(warehouse="Work In Progress - _KTA")
		self.run_queue()
		self.assertEqual(self.outbound(), [])
		self.assertEqual(self.fake_a.writes, [])

	def test_item_that_is_not_an_ingredient(self):
		if not frappe.db.exists("Item", "_KTEST-NAPKINS"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "_KTEST-NAPKINS",
					"item_group": "_Koomyo Ingredients",
					"stock_uom": "Nos",
					"is_stock_item": 1,
				}
			).insert()
		self.make_po(item="_KTEST-NAPKINS")
		self.run_queue()
		(log,) = self.outbound()
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn("_KTEST-NAPKINS", log.message)
		self.assertEqual(self.writes("/purchase-orders"), [])

	def test_koomyo_down_then_retry_is_a_replay(self):
		self.fake_a.scripted["/purchase-orders"] = [error_response(500, "internal_error") for _ in range(4)]
		po = self.make_po()
		with self.koomyo():
			original = events.KoomyoClient._backoff
			events.KoomyoClient._backoff = staticmethod(lambda attempt: 0)
			try:
				events.process_pending(self.conn_a.name)
			finally:
				events.KoomyoClient._backoff = original
		(log,) = self.outbound()
		self.assertEqual((log.status, log.error_class), ("Failed", "Retryable"))
		self.assertIsNone(frappe.db.get_value("Purchase Order", po.name, "koomyo_id"))
		frappe.db.set_value("Koomyo Event Log", log.name, "next_retry_on", "2026-01-01 00:00:00")
		self.run_queue()
		self.assertEqual(frappe.db.get_value("Koomyo Event Log", log.name, "status"), "Done")
		keys = {w["key"] for w in self.writes("/suppliers")}
		self.assertEqual(len(keys), 1, "the retry sends the same Idempotency-Key")
		self.assertEqual(len(self.fake_a.purchase_orders), 1)

	def test_dry_run_sends_nothing(self):
		self.conn_a.reload()
		self.conn_a.dry_run = 1
		self.conn_a.save()
		self.make_po()
		self.run_queue()
		(log,) = self.outbound()
		self.assertEqual((log.status, log.dry_run), ("Skipped", 1))
		self.assertIn("would send POST /suppliers", log.message)
		self.assertEqual(self.fake_a.writes, [])

	def test_supplier_change_updates_koomyo(self):
		self.make_po()
		self.run_queue()
		supplier = frappe.get_doc("Supplier", "_Koomyo Dairy")
		supplier.supplier_name = "_Koomyo Dairy Renamed"
		supplier.save()
		frappe.db.commit()
		self.run_queue()
		self.assertEqual(len(self.fake_a.suppliers), 1)
		self.assertEqual(self.fake_a.suppliers["sup-1"]["name"], "_Koomyo Dairy Renamed")
		frappe.db.set_value("Supplier", "_Koomyo Dairy", "supplier_name", "_Koomyo Dairy")

	def test_ingredient_cost_push(self):
		item = frappe.get_doc("Item", self.cheese)
		item.valuation_rate = 1512.3456
		item.save()
		frappe.db.commit()
		self.run_queue()
		(call,) = self.writes("/ingredients/")
		self.assertEqual((call["method"], call["body"]), ("PATCH", {"cost_per_unit": "1512.3456"}))
		self.assertEqual(self.fake_a.ingredients[MOZZARELLA]["cost_per_unit"], "1512.3456")


class TestReceipts(PurchasingTestCase):
	def received_event(self, order, seq=90):
		event = load_fixture("event_purchase_received")
		event.update(
			{"tenant_id": TENANT_A, "seq": seq, "id": f"evt-received-{seq}", "data": copy.deepcopy(order)}
		)
		return event

	def test_partial_receipt_goes_to_koomyo_and_its_event_changes_nothing(self):
		po = self.make_po(qty=10)
		self.run_queue()
		pr = self.receive(po, 8)
		self.run_queue()
		receipt_log = next(l for l in self.outbound() if l.event_type == "erpnext.purchase_receipt")
		self.assertEqual(receipt_log.status, "Done", receipt_log.message)
		(call,) = self.writes("/purchase-orders/po-1/receive")
		self.assertEqual(call["body"], {"lines": [{"line_id": "po-1-line-0", "quantity_received": "8"}]})
		order = self.fake_a.purchase_orders["po-1"]
		self.assertEqual(order["status"], "received")
		# Koomyo now sends purchase.received for our own receipt: nothing more is booked.
		log = self.book(self.received_event(order))
		self.assertEqual(log.status, "Skipped")
		self.assertIn(pr.name, log.message)
		self.assertEqual(
			len(frappe.get_all("Purchase Receipt", filters={"company": COMPANY_A, "docstatus": 1})), 1
		)

	def test_second_receipt_is_refused_clearly(self):
		po = self.make_po(qty=10)
		self.run_queue()
		self.receive(po, 6)
		self.receive(po, 4)
		self.run_queue()
		logs = [l for l in self.outbound() if l.event_type == "erpnext.purchase_receipt"]
		self.assertEqual(sorted(l.status for l in logs), ["Done", "Failed"])
		failed = next(l for l in logs if l.status == "Failed")
		self.assertIn("one receipt per order", failed.message)

	def test_received_in_koomyo_admin_makes_the_receipt(self):
		po = self.make_po(qty=10)
		self.run_queue()
		order = self.fake_a.purchase_orders["po-1"]
		order["status"] = "received"
		order["lines"][0]["quantity_received"] = "9.5"
		log = self.book(self.received_event(order))
		self.assertEqual(log.status, "Done", log.message)
		pr = frappe.get_doc("Purchase Receipt", log.reference_name)
		self.assertEqual((pr.docstatus, pr.koomyo_id, flt(pr.items[0].qty, 3)), (1, "po-1", 9.5))
		self.assertEqual(pr.items[0].purchase_order, po.name)
		self.run_queue()
		self.assertEqual(
			self.writes("/purchase-orders/po-1/receive"), [], "a receipt from Koomyo is not sent back"
		)

	def test_order_made_in_koomyo_is_skipped(self):
		order = load_fixture("event_purchase_received")["data"]
		order["external_refs"] = {}
		log = self.book(self.received_event(order))
		self.assertEqual(log.status, "Skipped")
		self.assertIn("made in Koomyo", log.message)
