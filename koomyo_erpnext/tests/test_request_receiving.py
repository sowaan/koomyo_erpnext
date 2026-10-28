"""Section 3: receiving per destination, in both directions.

An order with branch shares is received once for each place. ERPNext Purchase Receipts go out as one call per
destination (the line's total AT that place), and Koomyo deliveries come in as one Purchase Receipt per
delivery into the Warehouse of the place it landed at.
"""

from decimal import Decimal as D

import frappe
from frappe.utils import flt

from koomyo_erpnext.sync import purchasing
from koomyo_erpnext.tests.fake_koomyo import error_response
from koomyo_erpnext.tests.test_purchase_orders import PurchaseOrderCase, event_for, make_order
from koomyo_erpnext.tests.test_purchase_requests import MOZZARELLA
from koomyo_erpnext.tests.test_request_orders import (
	BRANCH_1,
	BRANCH_2,
	WAREHOUSE_2,
	RequestOrderCase,
)

CENTRAL = "6ed65fb7-3cdb-490f-b0a6-6a4d7096dcc4"
CENTRAL_WAREHOUSE = "Finished Goods - _KTA"  # where the test setup maps Koomyo's central warehouse
STORES = "Stores - _KTA"
LINE = "a93db756-ed1d-4386-9fb9-fbb95c7b9aca"


def split_receipt(po, quantities):
	"""A Purchase Receipt of an order, with `quantities` {warehouse: qty} (rows for other places removed)."""
	from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

	pr = make_purchase_receipt(po.name)
	pr.posting_date = "2026-10-27"
	pr.set_posting_time = 1
	for row in list(pr.items):
		if row.warehouse in quantities:
			row.qty = row.received_qty = quantities[row.warehouse]
		else:
			pr.remove(row)
	pr.insert()
	pr.submit()
	frappe.db.commit()
	return pr


class TestSendingPerDestination(RequestOrderCase):
	def setUp(self):
		super().setUp()
		self.conn_a.reload()
		self.conn_a.deliver_to = "Branches"
		self.conn_a.save()
		self.po = self.make_order()
		self.run_queue()
		self.fake_a.writes.clear()

	def receive_calls(self):
		return [w["body"] for w in self.writes("/purchase-orders/po-1/receive")]

	def test_each_receipt_is_a_delivery_at_its_own_place_with_the_total_there(self):
		first = split_receipt(self.po, {STORES: 5.5})
		self.run_queue()
		(call,) = self.receive_calls()
		self.assertEqual((call["destination_type"], call["destination_id"]), ("branch", BRANCH_1))
		self.assertEqual(call["lines"][0]["quantity_received"], "5.5")
		order = self.fake_a.purchase_orders["po-1"]
		self.assertEqual(order["status"], "partial", "Johar Town's share has not come")
		self.assertEqual(order["receipts"][-1]["location_id"], BRANCH_1)
		self.assertEqual(frappe.db.get_value("Purchase Receipt", first.name, "koomyo_id"), "po-1-rcpt-1")

		second = split_receipt(self.po, {WAREHOUSE_2: 5})
		self.run_queue()
		call = self.receive_calls()[-1]
		self.assertEqual((call["destination_type"], call["destination_id"]), ("branch", BRANCH_2))
		self.assertEqual(
			call["lines"][0]["quantity_received"], "5", "Gulberg's 5.5 is not in Johar Town's total"
		)
		self.assertEqual(frappe.db.get_value("Purchase Receipt", second.name, "koomyo_id"), "po-1-rcpt-2")

	def test_a_later_delivery_to_the_same_place_sends_the_new_total_there(self):
		split_receipt(self.po, {STORES: 4})
		self.run_queue()
		split_receipt(self.po, {STORES: 1.5})
		self.run_queue()
		self.assertEqual([c["lines"][0]["quantity_received"] for c in self.receive_calls()], ["4", "5.5"])

	def test_one_receipt_for_two_places_is_two_deliveries_and_none_is_booked_twice(self):
		pr = split_receipt(self.po, {STORES: 6, WAREHOUSE_2: 5})
		self.run_queue()
		calls = self.receive_calls()
		self.assertEqual(sorted(c["destination_id"] for c in calls), sorted([BRANCH_1, BRANCH_2]))
		order = self.fake_a.purchase_orders["po-1"]
		self.assertEqual(order["status"], "received")
		doc = frappe.get_doc("Purchase Receipt", pr.name)
		self.assertEqual(doc.koomyo_id, "po-1-rcpt-1")
		self.assertEqual(doc.koomyo_extra_ids.split(), ["po-1-rcpt-2"])
		# Koomyo sends purchase.received for both: nothing is booked again
		for i in (1, 2):
			partial = dict(
				order,
				receipts=order["receipts"][:i],
				status="partial",
				external_refs={"erpnext": self.po.name},
			)
			log = self.book(event_for(partial, "purchase.received", 700 + i))
			self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		receipts = frappe.get_all(
			"Purchase Receipt", filters={"company": self.conn_a.company, "docstatus": 1}
		)
		self.assertEqual(len(receipts), 1)

	def test_a_warehouse_that_is_not_on_the_branch_map_names_the_row_to_add(self):
		unmapped = split_receipt(self.po, {WAREHOUSE_2: 1})
		frappe.db.set_value(
			"Purchase Receipt Item", {"parent": unmapped.name}, "warehouse", "Goods In Transit - _KTA"
		)
		frappe.db.commit()
		purchasing.store_outbound(
			self.conn_a,
			"erpnext.purchase_receipt",
			frappe.get_doc("Purchase Receipt", unmapped.name),
			action="again",
			extra={"purchase_order": self.po.name},
		)
		self.run_queue()
		failed = [
			l for l in self.outbound() if l.status == "Failed" and l.event_type == "erpnext.purchase_receipt"
		]
		self.assertTrue(failed)
		self.assertEqual(failed[-1].error_class, "NeedsMapping")
		self.assertIn("Goods In Transit - _KTA", failed[-1].message)

	def test_a_closed_order_finishes_short_every_place_still_waiting(self):
		split_receipt(self.po, {STORES: 5.5})
		self.run_queue()
		frappe.get_doc("Purchase Order", self.po.name).update_status("Closed")
		frappe.db.commit()
		self.assertEqual(purchasing.queue_closed_orders(self.conn_a.name), 1)
		self.run_queue()
		closes = [c for c in self.receive_calls() if c.get("close_short")]
		self.assertEqual(sorted(c["destination_id"] for c in closes), sorted([BRANCH_1, BRANCH_2]))
		gulberg = next(c for c in closes if c["destination_id"] == BRANCH_1)
		self.assertEqual(
			gulberg["lines"][0]["quantity_received"], "5.5", "the total so far, so nothing more is booked"
		)
		order = self.fake_a.purchase_orders["po-1"]
		self.assertEqual(order["status"], "received")
		self.assertTrue(all(a["closed_short"] for a in order["lines"][0]["allocations"]))

	def test_nothing_new_at_one_place_does_not_stop_the_other(self):
		split_receipt(self.po, {STORES: 4})
		self.run_queue()
		self.fake_a.writes.clear()
		both = split_receipt(self.po, {STORES: 0.5, WAREHOUSE_2: 5})
		# the first call of this receipt (Gulberg) finds nothing new; the second (Johar Town) goes through
		self.fake_a.scripted["/purchase-orders/po-1/receive"] = [
			error_response(409, "nothing_to_receive", "nothing new")
		]
		self.run_queue()
		logs = {l.entity_id: l.status for l in self.outbound() if l.event_type == "erpnext.purchase_receipt"}
		self.assertEqual(logs[both.name], "Done")
		self.assertEqual([c["destination_id"] for c in self.receive_calls()], [BRANCH_1, BRANCH_2][1:])


class TestBookingPerDestination(PurchaseOrderCase):
	def setUp(self):
		super().setUp()
		self.conn_a.reload()
		self.conn_a.append(
			"branch_map",
			{
				"location_type": "Branch",
				"koomyo_branch_id": BRANCH_2,
				"koomyo_branch_name": "Johar Town",
				"cost_center": self.conn_a.branch_map[0].cost_center,
				"warehouse": WAREHOUSE_2,
			},
		)
		self.conn_a.save()

	def share(self, branch, quantity, kind="branch", place=None, sid=None):
		return {
			"id": sid or f"a-{branch[-1]}-{kind}",
			"branch_id": branch,
			"branch_name": "B",
			"quantity": quantity,
			"destination_type": kind,
			"destination_id": place or (branch if kind == "branch" else CENTRAL),
			"destination_name": None,
			"request_line_id": None,
			"received_quantity": "0",
			"landed_quantity": "0",
			"distributed_quantity": "0",
			"lost_quantity": "0",
			"closed_short": False,
			"short_reason": None,
		}

	def shared_order(self, shares, status="ordered", updated="2026-10-26T10:00:00Z", **extra):
		total = sum(D(s["quantity"]) for s in shares)
		order = make_order(status, updated, branch_id=None, total_ordered=str(total * D("1450")), **extra)
		order["lines"][0].update(
			{
				"quantity_ordered": str(total),
				"ordered_value": str(total * D("1450")),
				"line_total": str(total * D("1450")),
				"allocations": shares,
			}
		)
		return order

	def delivery(self, rid, place_type, place, qty, at="2026-10-27T09:00:00Z"):
		return {
			"id": rid,
			"received_at": at,
			"received_by": "Sam",
			"delivery_note": f"DN-{rid}",
			"closed_short": False,
			"location_type": place_type,
			"location_id": place,
			"location_name": None,
			"lines": [
				{
					"line_id": LINE,
					"ingredient_id": MOZZARELLA,
					"quantity": qty,
					"reason_code": None,
					"note": None,
				}
			],
		}

	def books(self, order, event_type="purchase.updated"):
		self.po_seq = getattr(self, "po_seq", 800) + 1
		return self.book(event_for(order, event_type, self.po_seq))

	def po(self):
		name = frappe.db.get_value(
			"Purchase Order",
			{"koomyo_connection": self.conn_a.name, "koomyo_id": "26711726-8300-4adc-a447-2a6213d914fa"},
		)
		return frappe.get_doc("Purchase Order", name) if name else None

	def stock_at(self, warehouse):
		return D(
			str(
				frappe.db.get_value("Bin", {"item_code": self.cheese, "warehouse": warehouse}, "actual_qty")
				or 0
			)
		)

	def test_an_order_with_shares_is_one_row_per_share_at_its_own_warehouse(self):
		order = self.shared_order(
			[
				self.share(BRANCH_1, "6"),
				self.share(BRANCH_2, "5"),
				self.share(BRANCH_1, "2", kind="warehouse", sid="w"),
			]
		)
		log = self.books(order)
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		po = self.po()
		self.assertEqual(
			[(r.warehouse, flt(r.qty, 3)) for r in po.items],
			[(STORES, 6), (WAREHOUSE_2, 5), (CENTRAL_WAREHOUSE, 2)],
		)
		self.assertFalse(po.set_warehouse)
		self.assertEqual(D(str(po.grand_total)).quantize(D("0.01")), D("18850.00"))

	def test_each_delivery_lands_at_its_own_place_and_nowhere_else(self):
		before = {w: self.stock_at(w) for w in (STORES, WAREHOUSE_2, CENTRAL_WAREHOUSE)}
		order = self.shared_order([self.share(BRANCH_1, "6"), self.share(BRANCH_2, "5")])
		self.books(order)
		one = self.delivery("rcpt-1", "branch", BRANCH_1, "5.5")
		o1 = dict(
			order, status="partial", updated_at="2026-10-27T09:00:00Z", receipts=[one], fully_received=False
		)
		log = self.books(o1, "purchase.received")
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		pr = frappe.get_doc("Purchase Receipt", log.reference_name)
		self.assertEqual([(r.warehouse, flt(r.qty, 3)) for r in pr.items], [(STORES, 5.5)])
		self.assertEqual(pr.koomyo_id, "rcpt-1")
		self.assertEqual(self.stock_at(STORES) - before[STORES], D("5.5"))
		self.assertEqual(self.stock_at(WAREHOUSE_2), before[WAREHOUSE_2], "nobody else's stock moved")

		two = self.delivery("rcpt-2", "branch", BRANCH_2, "5", at="2026-10-28T09:00:00Z")
		o2 = dict(
			order,
			status="received",
			updated_at="2026-10-28T09:00:00Z",
			receipts=[one, two],
			fully_received=True,
		)
		log = self.books(o2, "purchase.received")
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		self.assertEqual(self.stock_at(WAREHOUSE_2) - before[WAREHOUSE_2], D("5"))
		# a replay books nothing more
		self.assertEqual(self.books(o2, "purchase.received").status, "Done")
		booked = frappe.get_all(
			"Purchase Receipt", filters={"koomyo_connection": self.conn_a.name, "docstatus": 1}
		)
		self.assertEqual(len(booked), 2)

	def test_goods_for_the_warehouse_are_received_there_not_at_the_branches(self):
		before = self.stock_at(CENTRAL_WAREHOUSE)
		order = self.shared_order(
			[
				self.share(BRANCH_1, "6", kind="warehouse", sid="w1"),
				self.share(BRANCH_2, "5", kind="warehouse", sid="w2"),
			]
		)
		self.books(order)
		one = self.delivery("rcpt-1", "warehouse", CENTRAL, "11")
		log = self.books(
			dict(order, status="received", updated_at="2026-10-27T09:00:00Z", receipts=[one]),
			"purchase.received",
		)
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		pr = frappe.get_doc("Purchase Receipt", log.reference_name)
		self.assertEqual(sorted(flt(r.qty, 3) for r in pr.items), [5, 6], "row by row, each share its part")
		self.assertEqual({r.warehouse for r in pr.items}, {CENTRAL_WAREHOUSE})
		self.assertEqual(self.stock_at(CENTRAL_WAREHOUSE) - before, D("11"))

	def test_a_place_that_is_not_mapped_names_the_row(self):
		order = self.shared_order([self.share(BRANCH_1, "6")])
		self.books(order)
		one = self.delivery("rcpt-1", "warehouse", "unmapped-warehouse", "6")
		log = self.books(
			dict(order, status="received", receipts=[one], updated_at="2026-10-27T09:00:00Z"),
			"purchase.received",
		)
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn("unmapped-warehouse", log.message)

	def test_a_short_delivery_closed_short_is_just_a_smaller_receipt(self):
		order = self.shared_order([self.share(BRANCH_1, "6")])
		self.books(order)
		one = self.delivery("rcpt-1", "branch", BRANCH_1, "5.5")
		one["closed_short"] = True
		one["lines"][0]["reason_code"] = "short"
		closed = dict(
			order,
			status="received",
			closed_short=True,
			updated_at="2026-10-27T09:00:00Z",
			receipts=[one],
			fully_received=True,
		)
		log = self.books(closed, "purchase.received")
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertEqual(flt(frappe.get_doc("Purchase Receipt", log.reference_name).items[0].qty, 3), 5.5)


class TestClosingAPlace(RequestOrderCase):
	def setUp(self):
		super().setUp()
		self.conn_a.reload()
		self.conn_a.deliver_to = "Branches"
		self.conn_a.save()
		self.po = self.make_order()
		self.run_queue()
		self.fake_a.writes.clear()

	def closes(self):
		return [
			w["body"] for w in self.writes("/purchase-orders/po-1/receive") if w["body"].get("close_short")
		]

	def test_a_place_where_nothing_arrived_is_closed_with_zero_totals_and_books_no_delivery(self):
		frappe.get_doc("Purchase Order", self.po.name).update_status("Closed")
		frappe.db.commit()
		purchasing.queue_closed_orders(self.conn_a.name)
		self.run_queue()
		closes = self.closes()
		self.assertEqual(sorted(c["destination_id"] for c in closes), sorted([BRANCH_1, BRANCH_2]))
		for body in closes:
			self.assertEqual([l["quantity_received"] for l in body["lines"]], ["0"])
		order = self.fake_a.purchase_orders["po-1"]
		self.assertEqual(order["status"], "received")
		self.assertEqual({r["lines"] == [] and r["closed_short"] for r in order["receipts"]}, {True})
		self.assertEqual(frappe.get_all("Purchase Receipt", filters={"company": self.conn_a.company}), [])

	def test_sending_the_same_close_again_is_safe(self):
		frappe.get_doc("Purchase Order", self.po.name).update_status("Closed")
		frappe.db.commit()
		purchasing.queue_closed_orders(self.conn_a.name)
		self.run_queue()
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_order_close"]
		self.assertEqual(log.status, "Done")
		# the same close sent again (Koomyo's order status lagging): every place answers nothing_to_receive
		self.fake_a.purchase_orders["po-1"]["status"] = "partial"
		purchasing.store_outbound(
			self.conn_a,
			"erpnext.purchase_order_close",
			frappe.get_doc("Purchase Order", self.po.name),
			action="again",
		)
		self.run_queue()
		again = [
			l
			for l in self.outbound()
			if l.event_type == "erpnext.purchase_order_close" and l.name != log.name
		]
		self.assertEqual(len(again), 1)
		self.assertEqual(again[0].status, "Skipped", again[0].message)
		self.assertEqual(len(self.fake_a.purchase_orders["po-1"]["receipts"]), 2, "no third closing receipt")

	def test_one_place_closed_and_the_other_still_received(self):
		split_receipt(self.po, {STORES: 6})
		self.run_queue()
		frappe.get_doc("Purchase Order", self.po.name).update_status("Closed")
		frappe.db.commit()
		purchasing.queue_closed_orders(self.conn_a.name)
		self.run_queue()
		closes = self.closes()
		self.assertEqual(
			[c["destination_id"] for c in closes],
			[BRANCH_2],
			"Gulberg was complete: only Johar Town is closed",
		)
		self.assertEqual(closes[0]["lines"][0]["quantity_received"], "0")
