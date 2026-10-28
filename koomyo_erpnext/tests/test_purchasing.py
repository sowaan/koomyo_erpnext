"""Slice 5: ERPNext purchasing written to Koomyo, and purchase.received back.

Writes go to the fake Koomyo, which keeps every call (method, path, Idempotency-Key, body).
"""

import copy
from decimal import Decimal as D
from unittest.mock import patch

import frappe
from frappe.utils import flt

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.sync import events, purchasing
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

	def receive(self, po, qty, note=None):
		from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

		pr = make_purchase_receipt(po.name)
		if note:
			pr.supplier_delivery_note = note
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
		self.assertEqual(order["status"], "partial", "8 of 10 is not the whole order")
		# Koomyo now sends purchase.received for our own delivery: nothing more is booked.
		log = self.book(self.received_event(order))
		self.assertEqual(log.status, "Done", log.message)
		self.assertEqual(frappe.db.get_value("Purchase Receipt", pr.name, "koomyo_id"), "po-1-rcpt-1")
		self.assertEqual(
			len(frappe.get_all("Purchase Receipt", filters={"company": COMPANY_A, "docstatus": 1})), 1
		)

	def test_a_second_receipt_is_a_second_delivery_with_the_new_total(self):
		po = self.make_po(qty=10)
		self.run_queue()
		first = self.receive(po, 6)
		self.run_queue()
		second = self.receive(po, 4)
		self.run_queue()
		logs = [l for l in self.outbound() if l.event_type == "erpnext.purchase_receipt"]
		self.assertEqual([l.status for l in logs], ["Done", "Done"], [l.message for l in logs])
		bodies = [w["body"] for w in self.writes("/purchase-orders/po-1/receive")]
		self.assertEqual(
			[b["lines"][0]["quantity_received"] for b in bodies], ["6", "10"], "totals, not each delivery"
		)
		order = self.fake_a.purchase_orders["po-1"]
		self.assertEqual((order["status"], len(order["receipts"])), ("received", 2))
		self.assertEqual(
			[frappe.db.get_value("Purchase Receipt", n, "koomyo_id") for n in (first.name, second.name)],
			["po-1-rcpt-1", "po-1-rcpt-2"],
		)
		# the events Koomyo sends for them book nothing more
		for i in range(len(order["receipts"])):
			partial = dict(order, receipts=order["receipts"][: i + 1])
			self.assertEqual(self.book(self.received_event(partial, seq=91 + i)).status, "Done")
		self.assertEqual(
			len(frappe.get_all("Purchase Receipt", filters={"company": COMPANY_A, "docstatus": 1})), 2
		)

	def test_a_replay_of_the_same_receipt_sends_nothing_twice(self):
		po = self.make_po(qty=10)
		self.run_queue()
		self.receive(po, 6)
		self.run_queue()
		from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry

		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_receipt"]
		retry([log.name])
		self.run_queue()
		self.assertEqual(len(self.fake_a.purchase_orders["po-1"]["receipts"]), 1)
		again = frappe.get_doc("Koomyo Event Log", log.name)
		self.assertIn(again.status, ("Done", "Skipped"), again.message)

	def test_the_delivery_note_is_sent_and_a_closed_order_is_finished_short(self):
		po = self.make_po(qty=10)
		self.run_queue()
		pr = self.receive(po, 6, note="DN-77")
		self.run_queue()
		(call,) = self.writes("/purchase-orders/po-1/receive")
		self.assertEqual(call["body"]["delivery_note"], "DN-77")
		self.assertNotIn("close_short", call["body"])
		# a person closes the order in ERPNext: Koomyo is told to finish it short
		doc = frappe.get_doc("Purchase Order", po.name)
		doc.update_status("Closed")
		frappe.db.commit()
		self.assertEqual(purchasing.queue_closed_orders(self.conn_a.name), 1)
		self.assertEqual(purchasing.queue_closed_orders(self.conn_a.name), 0, "queued once")
		self.run_queue()
		close = [w for w in self.writes("/purchase-orders/po-1/receive")][-1]["body"]
		self.assertTrue(close["close_short"])
		self.assertEqual(close["lines"], [{"line_id": "po-1-line-0", "quantity_received": "6"}])
		order = self.fake_a.purchase_orders["po-1"]
		self.assertEqual((order["status"], order["closed_short"]), ("received", True))
		self.assertEqual(
			pr.name, frappe.get_all("Purchase Receipt", filters={"company": COMPANY_A}, pluck="name")[0]
		)

	def test_closing_an_order_nothing_arrived_for_sends_nothing(self):
		po = self.make_po(qty=10)
		self.run_queue()
		frappe.get_doc("Purchase Order", po.name).update_status("Closed")
		frappe.db.commit()
		purchasing.queue_closed_orders(self.conn_a.name)
		self.run_queue()
		self.assertEqual(self.writes("/purchase-orders/po-1/receive"), [])
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_order_close"]
		self.assertEqual(log.status, "Skipped")

	def test_nothing_new_is_skipped_and_a_complete_order_is_said_plainly(self):
		po = self.make_po(qty=10)
		self.run_queue()
		self.fake_a.scripted["/purchase-orders/po-1/receive"] = [
			error_response(409, "nothing_to_receive", "nothing new")
		]
		self.receive(po, 3)
		self.run_queue()
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_receipt"]
		self.assertEqual(log.status, "Skipped")
		self.assertIn("already holds these quantities", log.message)
		# the order is complete in Koomyo: a further ERPNext receipt cannot become a delivery
		order = self.fake_a.purchase_orders["po-1"]
		order["status"] = "received"
		self.receive(po, 2)
		self.run_queue()
		failed = [
			l for l in self.outbound() if l.event_type == "erpnext.purchase_receipt" and l.status == "Failed"
		]
		self.assertEqual(len(failed), 1)
		self.assertIn("fully received or closed short", failed[0].message)

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


class TestCancelInErpnext(PurchasingTestCase):
	def cancel_in_erpnext(self, po):
		frappe.get_doc("Purchase Order", po.name).cancel()
		frappe.db.commit()
		self.run_queue()

	def cancel_log(self):
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_order_cancel"]
		return log

	def test_koomyo_is_told_with_a_reason_and_the_order_is_noted(self):
		po = self.make_po()
		self.run_queue()
		self.assertEqual(self.fake_a.purchase_orders["po-1"]["status"], "ordered")
		self.cancel_in_erpnext(po)
		log = self.cancel_log()
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		order = self.fake_a.purchase_orders["po-1"]
		self.assertEqual(order["status"], "cancelled")
		self.assertEqual(order["cancel_reason"], f"Cancelled in ERPNext ({po.name})")
		(call,) = self.writes("/purchase-orders/po-1/cancel")
		self.assertEqual(call["body"], {"reason": f"Cancelled in ERPNext ({po.name})"})
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Purchase Order", "reference_name": po.name},
			pluck="content",
		)
		self.assertTrue([c for c in comments if "Cancelled in Koomyo too" in c])

	def test_the_purchase_cancelled_event_that_comes_back_changes_nothing(self):
		from koomyo_erpnext.tests.test_purchase_orders import event_for

		po = self.make_po()
		self.run_queue()
		self.cancel_in_erpnext(po)
		order = dict(self.fake_a.purchase_orders["po-1"], external_refs={"erpnext": po.name})
		log = self.book(event_for(order, "purchase.cancelled", 960))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertEqual(frappe.db.get_value("Purchase Order", po.name, "docstatus"), 2)

	def test_a_partly_received_order_says_how_to_finish_it(self):
		po = self.make_po()
		self.run_queue()
		self.fake_a.purchase_orders["po-1"]["status"] = "partial"
		self.cancel_in_erpnext(po)
		log = self.cancel_log()
		self.assertEqual((log.status, log.error_class), ("Failed", "Fatal"))
		self.assertIn("partly received", log.message)
		self.assertIn("close", log.message)

	def test_an_order_already_received_or_cancelled(self):
		po = self.make_po()
		self.run_queue()
		self.fake_a.purchase_orders["po-1"]["status"] = "received"
		self.cancel_in_erpnext(po)
		log = self.cancel_log()
		self.assertEqual(log.error_class, "Fatal")
		self.assertIn("already received or closed short", log.message)
		# cancelled in the Koomyo admin first: already_cancelled is the job done
		self.fake_a.purchase_orders["po-1"]["status"] = "cancelled"
		from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry

		retry([log.name])
		self.run_queue()
		self.assertEqual(frappe.db.get_value("Koomyo Event Log", log.name, "status"), "Done")

	def test_a_retry_after_a_refusal_uses_a_new_key(self):
		from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry

		po = self.make_po()
		self.run_queue()
		self.fake_a.purchase_orders["po-1"]["status"] = "partial"
		self.cancel_in_erpnext(po)
		log = self.cancel_log()
		self.fake_a.purchase_orders["po-1"]["status"] = "ordered"
		retry([log.name])
		self.run_queue()
		self.assertEqual(frappe.db.get_value("Koomyo Event Log", log.name, "status"), "Done")

	def test_an_order_never_sent_or_made_by_koomyo_says_nothing(self):
		unsent = self.make_po()
		frappe.db.set_value("Purchase Order", unsent.name, {"koomyo_connection": None, "koomyo_id": None})
		self.cancel_in_erpnext(unsent)
		self.assertEqual([l for l in self.outbound() if l.event_type == "erpnext.purchase_order_cancel"], [])

	def test_read_only_sends_nothing(self):
		po = self.make_po()
		self.run_queue()
		self.conn_a.reload()
		self.conn_a.access_mode = "Read Only"
		self.conn_a.save()
		self.cancel_in_erpnext(po)
		self.assertEqual(self.fake_a.cancel_calls, [])


class TestTaxAndChargesOutbound(PurchasingTestCase):
	def accounts(self):
		from koomyo_erpnext.tests.helpers import ensure_account

		self.conn_a.reload()
		acc = {
			"tax": ensure_account(COMPANY_A, "_KTA", "Input Tax", "Duties and Taxes", "Tax"),
			"withholding": ensure_account(
				COMPANY_A, "_KTA", "Withholding Payable", "Duties and Taxes", "Tax"
			),
			"landed": ensure_account(
				COMPANY_A, "_KTA", "Landed Cost Clearing", "Direct Expenses", "Expenses Included In Valuation"
			),
			"charges": ensure_account(COMPANY_A, "_KTA", "Purchase Charges", "Indirect Expenses"),
		}
		self.conn_a.update(
			{
				"purchase_tax_account": acc["tax"],
				"purchase_withholding_account": acc["withholding"],
				"purchase_charge_account": acc["landed"],
				"purchase_charge_expense_account": acc["charges"],
			}
		)
		self.conn_a.save()
		return acc

	def taxed_order(self, rows):
		po = self.make_po(submit=False)
		for row in rows:
			po.append("taxes", row)
		po.save()
		po.submit()
		frappe.db.commit()
		return po

	def sent(self):
		(call,) = [w for w in self.writes("/purchase-orders") if w["path"] == "/purchase-orders"]
		return call["body"]

	def test_tax_withholding_and_charges_go_as_inputs_and_koomyos_total_is_read(self):
		acc = self.accounts()
		po = self.taxed_order(
			[
				{
					"charge_type": "On Net Total",
					"account_head": acc["tax"],
					"description": "Sales tax",
					"rate": 18,
					"category": "Total",
					"add_deduct_tax": "Add",
				},
				{
					"charge_type": "Actual",
					"account_head": acc["landed"],
					"description": "Freight",
					"tax_amount": 1000,
					"category": "Valuation and Total",
					"add_deduct_tax": "Add",
				},
				{
					"charge_type": "Actual",
					"account_head": acc["charges"],
					"description": "Loading",
					"tax_amount": 200,
					"category": "Total",
					"add_deduct_tax": "Add",
				},
				{
					"charge_type": "Actual",
					"account_head": acc["withholding"],
					"description": "Withholding",
					"tax_amount": 145,
					"category": "Total",
					"add_deduct_tax": "Deduct",
				},
			]
		)
		self.run_queue()
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_order"]
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		body = self.sent()
		self.assertEqual(
			(body["tax_name"], body["tax_rate"], body["tax_mode"], body["tax_recoverable"]),
			("Sales tax", "18", "exclusive", True),
		)
		by_label = {c["label"]: c for c in body["charges"]}
		self.assertEqual(
			(
				by_label["Freight"]["spread_over_cost"],
				by_label["Freight"]["value"],
				by_label["Freight"]["direction"],
			),
			("value", "1000", "add"),
		)
		self.assertEqual(by_label["Loading"]["spread_over_cost"], "none")
		self.assertEqual(
			(
				by_label["Withholding"]["role"],
				by_label["Withholding"]["direction"],
				by_label["Withholding"]["value"],
			),
			("withholding", "deduct", "145"),
		)
		self.assertEqual(body["lines"][0]["unit_cost"], "1450", "the price is without tax")
		# 14500 + 18% + 1000 + 200 - 145 = 18165: Koomyo's figure is read and noted, not computed here
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Purchase Order", "reference_name": po.name},
			pluck="content",
		)
		self.assertTrue([c for c in comments if "Koomyo's total for this order is 18165.00" in c], comments)
		self.assertNotIn("more than rounding", log.message or "")

	def test_a_tax_that_cannot_be_claimed_back_and_an_inclusive_price(self):
		acc = self.accounts()
		self.taxed_order(
			[
				{
					"charge_type": "On Net Total",
					"account_head": acc["tax"],
					"description": "GST",
					"rate": 18,
					"category": "Valuation and Total",
					"add_deduct_tax": "Add",
					"included_in_print_rate": 1,
				}
			]
		)
		self.run_queue()
		body = self.sent()
		self.assertEqual((body["tax_mode"], body["tax_recoverable"]), ("inclusive", False))

	def test_the_connection_decides_how_charges_are_spread(self):
		acc = self.accounts()
		self.conn_a.reload()
		self.conn_a.landed_cost_method = "By Quantity"
		self.conn_a.save()
		self.taxed_order(
			[
				{
					"charge_type": "Actual",
					"account_head": acc["landed"],
					"description": "Freight",
					"tax_amount": 1000,
					"category": "Valuation and Total",
					"add_deduct_tax": "Add",
				}
			]
		)
		self.run_queue()
		self.assertEqual(self.sent()["charges"][0]["spread_over_cost"], "quantity")

	def test_a_line_discount_goes_as_a_percent_on_the_price_before_it(self):
		po = self.make_po(submit=False)
		po.items[0].discount_percentage = 10
		po.save()
		po.submit()
		frappe.db.commit()
		self.run_queue()
		line = self.sent()["lines"][0]
		self.assertEqual((line["unit_cost"], line["discount_percent"]), ("1450", "10"))

	def test_what_cannot_be_said_stays_in_the_notes_with_a_warning(self):
		acc = self.accounts()
		self.taxed_order(
			[
				{
					"charge_type": "On Net Total",
					"account_head": acc["tax"],
					"description": "Sales tax",
					"rate": 18,
					"category": "Total",
					"add_deduct_tax": "Add",
				},
				{
					"charge_type": "On Net Total",
					"account_head": acc["tax"],
					"description": "Provincial tax",
					"rate": 2,
					"category": "Total",
					"add_deduct_tax": "Add",
				},
			]
		)
		self.run_queue()
		body = self.sent()
		self.assertEqual(body["tax_rate"], "18")
		self.assertIn("Provincial tax", body["notes"])
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_order"]
		self.assertIn("Not everything in the taxes and charges", log.message)

	def test_an_order_without_taxes_sends_its_remarks_and_no_warning(self):
		self.make_po()
		self.run_queue()
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_order"]
		self.assertEqual((log.status, log.message or ""), ("Done", ""))
		body = self.sent()
		for key in ("tax_rate", "tax_name", "charges"):
			self.assertNotIn(key, body)
