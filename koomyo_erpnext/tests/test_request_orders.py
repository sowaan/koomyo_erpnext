"""Section 2: a Purchase Order made from Koomyo purchase requests is sent with a share for each request line.

Two branches asked for mozzarella (Gulberg 6 kg approved, Johar Town 5 kg): one ERPNext Purchase Order for
11 kg is one Koomyo line with two shares. Koomyo's checks are played by the fake (shares add up, the line is
approved, not more than approved), and a refusal creates nothing.
"""

import copy

import frappe
from frappe.utils import flt

from koomyo_erpnext.tests.test_purchase_requests import (
	CHEESE,
	LINE_1,
	LINE_2,
	MOZZARELLA,
	line,
	make_request,
	request_event,
)
from koomyo_erpnext.tests.test_purchasing import PurchasingTestCase

BRANCH_1 = "90000000-0000-4000-8000-000000000001"
BRANCH_2 = "90000000-0000-4000-8000-000000000002"
REQUEST_2 = "6e8b2d73-1c2f-4a1f-8e44-3a7a6b5c9f22"
WAREHOUSE_2 = "Work In Progress - _KTA"  # Johar Town (Finished Goods is the Koomyo central warehouse)


class RequestOrderCase(PurchasingTestCase):
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
		self.conn_a.sync_purchase_requests = 1
		self.conn_a.save()
		self.seq = 600
		self.book_requests()

	def book_requests(self):
		one = make_request(lines=[line(LINE_1, CHEESE, "8.000", "6.000")])
		two = make_request(
			id=REQUEST_2,
			branch_id=BRANCH_2,
			branch_name="Johar Town",
			number="PR-2026-0013",
			lines=[line(LINE_2, CHEESE, "5.000", "5.000")],
		)
		self.fake_a.lists["/purchase-requests"] = [one, two]
		for card in (one, two):
			self.seq += 1
			self.assertEqual(
				self.book(request_event(card, "purchase_request.approved", self.seq)).status, "Done"
			)
		self.fake_a.writes.clear()

	def material_rows(self):
		rows = []
		for request_id in ("5d7a1c62-0b1e-4f0e-9d33-2f6f5a4b8e00", REQUEST_2):
			name = frappe.db.get_value(
				"Material Request", {"koomyo_connection": self.conn_a.name, "koomyo_id": request_id}
			)
			rows += frappe.get_doc("Material Request", name).items
		return rows

	def make_order(self, rows=None, rate=1450, submit=True):
		rows = self.material_rows() if rows is None else rows
		po = frappe.get_doc(
			{
				"doctype": "Purchase Order",
				"company": self.conn_a.company,
				"supplier": "_Koomyo Dairy",
				"transaction_date": "2026-10-26",
				"schedule_date": "2026-11-02",
				"currency": "PKR",
				"conversion_rate": 1,
				"items": [
					{
						"item_code": row.item_code,
						"qty": row.qty,
						"rate": rate if not isinstance(rate, list | tuple) else rate[i],
						"warehouse": row.warehouse,
						"schedule_date": "2026-11-02",
						"material_request": row.parent,
						"material_request_item": row.name,
					}
					for i, row in enumerate(rows)
				],
			}
		).insert()
		if submit:
			po.submit()
		frappe.db.commit()
		return po

	def sent(self):
		return [w for w in self.writes("/purchase-orders") if w["path"] == "/purchase-orders"]


class TestSharesOnTheOrder(RequestOrderCase):
	def test_two_branches_make_one_line_with_one_share_each(self):
		po = self.make_order()
		self.run_queue()
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_order"]
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		(call,) = self.sent()
		body = call["body"]
		(line_body,) = body["lines"]
		self.assertEqual((line_body["ingredient_id"], line_body["quantity_ordered"]), (MOZZARELLA, "11"))
		self.assertEqual(
			[(a["request_line_id"], a["quantity"]) for a in line_body["allocations"]],
			[(LINE_1, "6"), (LINE_2, "5")],
		)
		self.assertNotIn("deliver_to", body, "each share names where it goes")
		self.assertEqual(
			[(a["destination_type"], a["destination_id"]) for a in line_body["allocations"]],
			[("branch", BRANCH_1), ("branch", BRANCH_2)],
			"the row's Warehouse is the branch's own: straight to the branch",
		)
		self.assertNotIn(
			"branch_id", body, "the shares carry the branches; the order has no place of its own"
		)
		self.assertEqual(frappe.db.get_value("Purchase Order", po.name, "koomyo_id"), "po-1")
		# Koomyo moved the request lines to ordered itself
		cards = {c["id"]: c for c in self.fake_a.lists["/purchase-requests"]}
		self.assertEqual({c["lines"][0]["status"] for c in cards.values()}, {"ordered"})
		order = self.fake_a.purchase_orders["po-1"]
		self.assertEqual(
			[(a["branch_id"], a["destination_type"]) for a in order["lines"][0]["allocations"]],
			[(BRANCH_1, "branch"), (BRANCH_2, "branch")],
		)

	def test_a_row_for_a_central_warehouse_sends_that_share_to_the_warehouse(self):
		rows = self.material_rows()
		po = self.make_order(submit=False)
		po.items[
			0
		].warehouse = "Finished Goods - _KTA"  # where the test setup maps Koomyo's central warehouse
		po.save()
		po.submit()
		frappe.db.commit()
		self.run_queue()
		(call,) = self.sent()
		places = {
			a["request_line_id"]: (a["destination_type"], a["destination_id"])
			for a in call["body"]["lines"][0]["allocations"]
		}
		self.assertEqual(places[LINE_1], ("warehouse", "6ed65fb7-3cdb-490f-b0a6-6a4d7096dcc4"))
		self.assertEqual(places[LINE_2], ("branch", BRANCH_2))
		self.assertTrue(rows)

	def test_a_row_at_another_branchs_warehouse_is_refused_before_anything_is_sent(self):
		po = self.make_order(submit=False)
		po.items[0].warehouse = WAREHOUSE_2  # Johar Town's, for Gulberg's request
		po.save()
		po.submit()
		frappe.db.commit()
		self.run_queue()
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_order"]
		self.assertEqual((log.status, log.error_class), ("Failed", "NeedsMapping"))
		self.assertIn("another branch", log.message)
		self.assertEqual(self.sent(), [])

	def test_the_receipt_then_goes_to_the_same_place_the_share_was_sent_to(self):
		from koomyo_erpnext.tests.test_request_receiving import split_receipt

		po = self.make_order()
		self.run_queue()
		self.fake_a.writes.clear()
		split_receipt(po, {"Stores - _KTA": 6})
		self.run_queue()
		(call,) = self.writes("/purchase-orders/po-1/receive")
		self.assertEqual(
			(call["body"]["destination_type"], call["body"]["destination_id"]), ("branch", BRANCH_1)
		)
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_receipt"]
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))

	def test_rows_of_one_item_with_one_rate_are_one_line_and_two_rates_are_refused(self):
		self.make_order(rate=[1450, 1500])
		self.run_queue()
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_order"]
		self.assertEqual((log.status, log.error_class), ("Failed", "Fatal"))
		self.assertIn("different rates", log.message)
		self.assertEqual(self.sent(), [], "nothing was sent")

	def test_a_row_from_no_request_in_an_order_that_fills_requests_is_refused(self):
		rows = self.material_rows()
		po = self.make_order(rows=rows[:1], submit=False)
		po.append(
			"items",
			{
				"item_code": rows[0].item_code,
				"qty": 1,
				"rate": 1450,
				"warehouse": rows[0].warehouse,
				"schedule_date": "2026-11-02",
			},
		)
		po.save()
		po.submit()
		frappe.db.commit()
		self.run_queue()
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_order"]
		self.assertEqual(log.error_class, "Fatal")
		self.assertIn("from no Koomyo Material Request", log.message)

	def test_an_order_with_no_requests_is_sent_as_before(self):
		self.make_po()
		self.run_queue()
		(call,) = self.sent()
		self.assertNotIn("deliver_to", call["body"])
		self.assertNotIn("allocations", call["body"]["lines"][0])
		self.assertEqual(call["body"]["branch_id"], BRANCH_1)


class TestRefusals(RequestOrderCase):
	def refused(self, expected_code, expected_words):
		self.make_order()
		self.run_queue()
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_order"]
		self.assertEqual((log.status, log.error_class), ("Failed", "Fatal"), log.message)
		self.assertIn(expected_words, log.message)
		self.assertEqual(self.fake_a.purchase_orders, {}, "a refusal creates nothing")
		return log

	def test_a_line_that_is_not_approved_any_more(self):
		self.fake_a.lists["/purchase-requests"][0]["lines"][0]["status"] = "submitted"
		self.refused("request_line_not_orderable", "is not approved")

	def test_more_than_purchasing_approved_names_the_other_orderer(self):
		self.fake_a.lists["/purchase-requests"][0]["lines"][0]["ordered_quantity"] = "4.000"
		self.refused("request_quantity_exceeded", "whoever orders a line first wins")

	def test_a_restaurant_without_the_feature(self):
		self.fake_a.purchase_requests_off = True
		self.refused("feature_not_available", "purchase requests feature")

	def test_a_share_of_another_ingredient(self):
		self.fake_a.lists["/purchase-requests"][1]["lines"][0]["ingredient"] = {
			"id": "other",
			"name": "X",
			"unit": "kg",
		}
		self.refused("invalid_parameter", "Check that its quantities match")

	def test_after_the_line_is_approved_again_retry_sends_it(self):
		self.fake_a.lists["/purchase-requests"][0]["lines"][0]["status"] = "submitted"
		self.make_order()
		self.run_queue()
		self.fake_a.lists["/purchase-requests"][0]["lines"][0]["status"] = "approved"
		from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry

		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_order"]
		retry([log.name])
		self.run_queue()
		after = frappe.get_doc("Koomyo Event Log", log.name)
		self.assertEqual(after.status, "Done", (after.message, after.attempts))
		self.assertEqual(len(self.fake_a.purchase_orders), 1)


class TestEditsComeBack(RequestOrderCase):
	def test_a_cancel_in_koomyo_cancels_the_order_and_the_request_can_be_ordered_again(self):
		from koomyo_erpnext.tests.test_purchase_orders import event_for

		po = self.make_order()
		self.run_queue()
		order = self.fake_a.purchase_orders["po-1"]
		cancelled = copy.deepcopy(order)
		cancelled.update(
			{
				"status": "cancelled",
				"updated_at": "2026-10-27T09:00:00Z",
				"cancel_reason": "Changed mind",
				"business_date": "2026-10-26",
				"external_refs": {"erpnext": po.name},
			}
		)
		self.seq += 1
		log = self.book(event_for(cancelled | {"object": "purchase_order"}, "purchase.cancelled", self.seq))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertEqual(frappe.db.get_value("Purchase Order", po.name, "docstatus"), 2)
		# the quantity went back to the requests: they are approved again and the Material Requests stay as they were
		names = {r.parent for r in self.material_rows()}
		for card in self.fake_a.lists["/purchase-requests"]:
			card["lines"][0].update({"status": "approved", "ordered_quantity": "0.000"})
			card["updated_at"] = "2026-10-27T09:00:05Z"
			self.seq += 1
			self.assertEqual(
				self.book(request_event(card, "purchase_request.approved", self.seq)).status, "Done"
			)
		self.assertEqual({r.parent for r in self.material_rows()}, names)


class TestCancelReturnsTheQuantity(RequestOrderCase):
	def test_cancelling_in_erpnext_puts_the_request_lines_back_to_approved(self):
		po = self.make_order()
		self.run_queue()
		cards = {c["id"]: c for c in self.fake_a.lists["/purchase-requests"]}
		self.assertEqual({c["lines"][0]["status"] for c in cards.values()}, {"ordered"})
		frappe.get_doc("Purchase Order", po.name).cancel()
		frappe.db.commit()
		self.run_queue()
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_order_cancel"]
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertEqual({c["lines"][0]["status"] for c in cards.values()}, {"approved"})
		self.assertEqual({float(c["lines"][0]["ordered_quantity"]) for c in cards.values()}, {0.0})
		# the order can be made again, and Koomyo takes it
		again = self.make_order()
		self.run_queue()
		self.assertTrue(frappe.db.get_value("Purchase Order", again.name, "koomyo_id"))
