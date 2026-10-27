"""Slice 5: ERPNext purchasing written to Koomyo, and purchase.received back.

Writes go to the fake Koomyo, which keeps every call (method, path, Idempotency-Key, body).
"""

import copy
from decimal import Decimal as D
from unittest.mock import patch

import frappe
from frappe.utils import flt

from koomyo_erpnext.client import KoomyoClient
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
			with patch.object(KoomyoClient, "_backoff", staticmethod(lambda attempt: 0)):
				events.process_pending(self.conn_a.name)
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
		self.assertEqual(log.status, "Done", log.message)
		self.assertEqual(frappe.db.get_value("Purchase Receipt", pr.name, "koomyo_id"), "po-1-rcpt-1")
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


class TestKoomyoMadeOrders(PurchasingTestCase):
	def setUp(self):
		super().setUp()
		self.conn_a.reload()
		self.conn_a.update(
			{
				"book_koomyo_purchases": 1,
				"main_warehouse": "Finished Goods - _KTA",
				"supplier_group": "_Koomyo Suppliers",
			}
		)
		self.conn_a.save()

	def koomyo_order(self, **changes):
		event = load_fixture("event_purchase_received")
		data = event["data"]
		data["external_refs"] = {}
		data["supplier"] = {"id": "koomyo-supplier-1", "name": "Koomyo Cheese Co"}
		data["lines"][0].update(
			{
				"ingredient_id": MOZZARELLA,
				"quantity_ordered": "10",
				"quantity_received": "9",
				"unit_cost": "1450",
				"ordered_value": "14500.00",
				"received_value": "13050.00",
				"line_total": "14500.00",
			}
		)
		data["total_ordered"] = "14500.00"
		data["receipts"][0]["lines"] = [
			{"line_id": data["lines"][0]["id"], "ingredient_id": MOZZARELLA, "quantity": "9"}
		]
		data.update(changes)
		event.update({"tenant_id": TENANT_A, "seq": 95, "id": "evt-koomyo-po"})
		return event

	def test_becomes_supplier_order_and_receipt(self):
		event = self.koomyo_order(branch_id=None)
		log = self.book(event)
		self.assertEqual(log.status, "Done", (log.message, log.error[-600:]))
		pr = frappe.get_doc("Purchase Receipt", log.reference_name)
		self.assertEqual(
			(pr.docstatus, pr.koomyo_id, flt(pr.items[0].qty, 3)), (1, event["data"]["receipts"][0]["id"], 9)
		)
		po = frappe.get_doc("Purchase Order", pr.items[0].purchase_order)
		self.assertEqual(
			(po.docstatus, po.koomyo_id, po.set_warehouse), (1, event["data"]["id"], "Finished Goods - _KTA")
		)
		self.assertEqual((flt(po.items[0].qty, 3), flt(po.items[0].rate, 2)), (10, 1450))
		supplier = frappe.get_doc("Supplier", po.supplier)
		self.assertEqual(
			(supplier.koomyo_connection, supplier.koomyo_id),
			(self.conn_a.name, event["data"]["supplier"]["id"]),
		)
		self.assertEqual(self.qty("Finished Goods - _KTA", MOZZARELLA), D("9"))

	def test_replay_and_no_echo_to_koomyo(self):
		event = self.koomyo_order()
		first = self.book(event)
		self.assertEqual(first.status, "Done", (first.message, first.error[-500:]))
		self.book(dict(event, id="resent", seq=96))
		self.run_queue()
		self.assertEqual(
			len(frappe.get_all("Purchase Order", filters={"koomyo_connection": self.conn_a.name})), 1
		)
		self.assertEqual(
			len(frappe.get_all("Purchase Receipt", filters={"koomyo_connection": self.conn_a.name})), 1
		)
		self.assertEqual(self.writes("/purchase-orders"), [], "a Koomyo order is never sent back")

	def test_off_by_default_skips(self):
		self.conn_a.reload()
		self.conn_a.book_koomyo_purchases = 0
		self.conn_a.save()
		log = self.book(self.koomyo_order())
		self.assertEqual(log.status, "Skipped")
		self.assertIn("Book Orders Made in Koomyo", log.message)


class TestReadOnly(PurchasingTestCase):
	"""Koomyo Access = Read Only: the app reads from Koomyo and never writes to it."""

	def set_access(self, mode):
		self.conn_a.reload()
		self.conn_a.access_mode = mode
		self.conn_a.save()

	def test_nothing_is_queued_or_written(self):
		self.set_access("Read Only")
		self.make_po()
		item = frappe.get_doc("Item", self.cheese)
		item.valuation_rate = 1999
		item.save()
		frappe.db.commit()
		self.run_queue()
		self.assertEqual(self.outbound(), [], "ERPNext-made documents are not even queued")
		self.assertEqual(self.fake_a.writes, [])

	def test_work_queued_before_is_skipped_then_sent_after_switching_back(self):
		self.make_po()  # queued while Read and Write
		self.set_access("Read Only")
		self.run_queue()
		(log,) = self.outbound()
		self.assertEqual(log.status, "Skipped")
		self.assertIn("Read Only", log.message)
		self.assertEqual(self.fake_a.writes, [])

		self.set_access("Read and Write")
		from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry

		retry([log.name])
		self.run_queue()
		self.assertEqual(frappe.db.get_value("Koomyo Event Log", log.name, "status"), "Done")
		self.assertEqual(len(self.writes("/purchase-orders")), 1)

	def test_reading_still_works_and_no_reference_is_written_back(self):
		self.set_access("Read Only")
		self.fake_a.lists["/categories"] = []
		self.fake_a.lists["/menu-items"] = []
		self.fake_a.lists["/ingredients"] = [self.fake_a.ingredients[MOZZARELLA]]
		self.fake_a.add_ingredient("new-ing", "Olives", "kg")
		self.fake_a.lists["/ingredients"].append(self.fake_a.ingredients["new-ing"])
		from koomyo_erpnext.sync import catalog

		with self.koomyo():
			summary = catalog.load_catalog(self.conn_a.name)
		self.assertEqual(summary["ingredients"]["created"], 1)
		self.assertEqual(self.fake_a.writes, [], "the ERPNext id is not written to Koomyo")
		log = self.book(self.koomyo_order_event())
		self.assertEqual(log.status, "Skipped", "a Koomyo-made order is read, then skipped here as it is off")

	def test_write_scopes_not_asked_for(self):
		from koomyo_erpnext.sync import events

		self.assertIn("purchasing.write", events.required_scopes(self.conn_a))
		self.set_access("Read Only")
		scopes = events.required_scopes(self.conn_a)
		self.assertFalse([s for s in scopes if s.endswith(".write")], scopes)
		self.assertIn("purchasing.read", scopes)
		self.fake_a.account["key"]["scopes"] = [
			s for s in self.fake_a.account["key"]["scopes"] if not s.endswith(".write")
		]
		with self.koomyo():
			result = self.conn_a.test_connection()
		self.assertEqual(result["missing_scopes"], [])

	def koomyo_order_event(self):
		event = load_fixture("event_purchase_received")
		event["data"]["external_refs"] = {}
		event.update({"tenant_id": TENANT_A, "seq": 97, "id": "evt-ro-po"})
		return event
