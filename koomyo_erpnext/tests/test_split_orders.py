"""A Purchase Order received at more than one Warehouse: shares by warehouse, one receive per place.

Koomyo splits a delivery across places only when the order's lines carry shares. Without them the whole delivery lands
at the order's own place, so the connector sends the split when the rows are in several Warehouses, refuses a receipt at
another place when the order has no shares, and re-sends the split with PATCH when the order is changed.
"""

import frappe

from koomyo_erpnext.tests.helpers import CENTRAL_WAREHOUSE
from koomyo_erpnext.tests.test_purchase_edits import update_items
from koomyo_erpnext.tests.test_purchase_requests import MOZZARELLA
from koomyo_erpnext.tests.test_purchasing import BRANCH, PurchasingTestCase

CENTRAL = "Finished Goods - _KTA"
STORES = "Stores - _KTA"


class SplitCase(PurchasingTestCase):
	def setUp(self):
		super().setUp()
		self.conn_a.reload()
		self.conn_a.warehouse_share_branch = (
			STORES  # the branch a share at the central warehouse is meant for
		)
		self.conn_a.save()

	def make_split_po(self, central=50, stores=70, submit=True):
		po = self.make_po(warehouse=STORES, qty=stores, submit=False)
		po.append(
			"items",
			{
				"item_code": po.items[0].item_code,
				"qty": central,
				"rate": 1450,
				"warehouse": CENTRAL,
				"schedule_date": "2026-10-13",
			},
		)
		po.save()
		if submit:
			po.submit()
		frappe.db.commit()
		return po

	def receive_at(self, po, quantities: dict):
		"""A Purchase Receipt of the given {warehouse: qty} (one row of the order each)."""
		from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

		pr = make_purchase_receipt(po.name)
		pr.posting_date = "2026-10-13"
		pr.set_posting_time = 1
		for row in list(pr.items):
			qty = quantities.get(row.warehouse)
			if not qty:
				pr.remove(row)
				continue
			row.qty = row.received_qty = qty
		pr.insert()
		pr.submit()
		frappe.db.commit()
		return pr

	def order_body(self):
		(call,) = self.created("/purchase-orders")
		return call["body"]


class TestSplitOrder(SplitCase):
	def test_rows_in_two_warehouses_are_one_line_with_a_share_for_each(self):
		self.make_split_po()
		self.run_queue()
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_order"]
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		body = self.order_body()
		(line,) = body["lines"]
		self.assertEqual((line["ingredient_id"], line["quantity_ordered"]), (MOZZARELLA, "120"))
		self.assertEqual(
			sorted(
				(a["destination_type"], a["destination_id"], a["quantity"], a.get("branch_id"))
				for a in line["allocations"]
			),
			sorted([("warehouse", CENTRAL_WAREHOUSE, "50", None), ("branch", BRANCH, "70", BRANCH)]),
			"a branch share names its branch; a share at a central warehouse names none",
		)
		self.assertTrue(all("request_line_id" not in a for a in line["allocations"]))
		self.assertNotIn("branch_id", body, "the order follows its shares")

	def test_the_order_answer_has_no_branch_on_the_warehouse_share_and_reading_it_back_is_fine(self):
		self.make_split_po()
		self.run_queue()
		shares = self.fake_a.purchase_orders["po-1"]["lines"][0]["allocations"]
		self.assertEqual(
			{a["destination_type"]: a["branch_id"] for a in shares}, {"warehouse": None, "branch": BRANCH}
		)
		po = frappe.get_all("Purchase Order", filters={"koomyo_id": "po-1"}, pluck="name")
		self.assertTrue(po, "the order was linked")
		from koomyo_erpnext.tests.test_purchase_orders import event_for, make_order

		order = make_order("ordered", id="po-1", external_refs={"erpnext": po[0]})
		order["lines"][0]["allocations"] = shares
		self.assertEqual(self.book(event_for(order, "purchase.updated", 990)).status, "Done")

	def test_a_single_warehouse_order_has_no_shares_as_before(self):
		self.make_po()
		self.run_queue()
		self.assertNotIn("allocations", self.order_body()["lines"][0])
		self.assertEqual(self.order_body()["branch_id"], BRANCH)

	def test_each_delivery_is_received_at_its_own_place(self):
		po = self.make_split_po()
		self.run_queue()
		self.receive_at(po, {CENTRAL: 50})
		self.run_queue()
		self.receive_at(po, {STORES: 70})
		self.run_queue()
		calls = [w["body"] for w in self.writes("/purchase-orders/po-1/receive")]
		self.assertEqual(
			[(c["destination_type"], c["destination_id"], c["lines"][0]["quantity_received"]) for c in calls],
			[("warehouse", CENTRAL_WAREHOUSE, "50"), ("branch", BRANCH, "70")],
		)
		order = self.fake_a.purchase_orders["po-1"]
		got = {a["destination_id"]: a["received_quantity"] for a in order["lines"][0]["allocations"]}
		self.assertEqual(got, {CENTRAL_WAREHOUSE: "50", BRANCH: "70"})
		self.assertEqual(order["status"], "received")

	def test_a_warehouse_koomyo_does_not_know_stops_the_order_with_the_row_to_add(self):
		po = self.make_po(warehouse=STORES, qty=70, submit=False)
		po.append(
			"items",
			{
				"item_code": po.items[0].item_code,
				"qty": 50,
				"rate": 1450,
				"warehouse": "Work In Progress - _KTA",
				"schedule_date": "2026-10-13",
			},
		)
		po.save()
		po.submit()
		frappe.db.commit()
		self.run_queue()
		(log,) = [l for l in self.outbound() if l.event_type == "erpnext.purchase_order"]
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn("Work In Progress - _KTA", log.message)
		self.assertEqual(self.created("/purchase-orders"), [])


class TestReceiptAtAnotherPlace(SplitCase):
	def other_place_receipt(self, po, warehouse, qty=50):
		from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

		other = make_purchase_receipt(po.name)
		other.posting_date = "2026-10-14"
		other.set_posting_time = 1
		for row in other.items:
			row.qty = row.received_qty = qty
			row.warehouse = warehouse
		other.insert()
		return other

	def test_an_order_without_shares_does_not_take_a_receipt_at_another_place_in_erpnext_either(self):
		po = self.make_po(warehouse=STORES, qty=120)
		self.run_queue()
		self.receive_at(po, {STORES: 70})  # fine: the order's own place
		self.run_queue()
		other = self.other_place_receipt(po, CENTRAL)
		with self.assertRaises(frappe.ValidationError) as caught:
			other.submit()
		self.assertIn("has no shares", str(caught.exception))
		self.assertEqual(frappe.db.get_value("Purchase Receipt", other.name, "docstatus"), 0)
		self.run_queue()
		self.assertEqual(len(self.writes("/purchase-orders/po-1/receive")), 1, "nothing more was received")

	def test_an_order_with_shares_takes_receipts_only_at_the_places_of_its_shares(self):
		po = self.make_split_po()
		self.run_queue()
		self.conn_a.reload()
		self.conn_a.append(
			"branch_map",
			{
				"location_type": "Branch",
				"koomyo_branch_id": "90000000-0000-4000-8000-0000000000aa",
				"warehouse": "Work In Progress - _KTA",
			},
		)
		self.conn_a.save()
		other = self.other_place_receipt(po, "Work In Progress - _KTA")
		with self.assertRaises(frappe.ValidationError) as caught:
			other.submit()
		self.assertIn("delivers only to the places of its shares", str(caught.exception))

	def test_when_koomyo_cannot_be_read_the_check_only_warns_and_the_push_still_refuses(self):
		po = self.make_po(warehouse=STORES, qty=120)
		self.run_queue()
		other = self.other_place_receipt(po, CENTRAL)
		self.fake_a.down = True
		other.submit()  # not blocked: Koomyo could not be asked
		frappe.db.commit()
		self.fake_a.down = False
		self.run_queue()
		failed = [
			l for l in self.outbound() if l.event_type == "erpnext.purchase_receipt" and l.status == "Failed"
		]
		self.assertEqual(len(failed), 1)
		self.assertIn("has no shares", failed[0].message)
		self.assertEqual(self.writes("/purchase-orders/po-1/receive"), [])

	def test_a_receipt_at_a_place_that_is_a_share_goes_through(self):
		po = self.make_split_po()
		self.run_queue()
		pr = self.receive_at(po, {CENTRAL: 50})
		self.assertEqual(frappe.db.get_value("Purchase Receipt", pr.name, "docstatus"), 1)


class TestSplitByEdit(SplitCase):
	def test_changing_the_order_into_two_warehouses_patches_the_shares(self):
		po = self.make_po(warehouse=STORES, qty=120)
		self.run_queue()
		self.fake_a.writes.clear()
		doc = frappe.get_doc("Purchase Order", po.name)
		row = doc.items[0]
		update_items(
			doc,
			[
				{"docname": row.name, "item_code": row.item_code, "qty": 70, "rate": 1450},
				{"docname": "", "item_code": row.item_code, "qty": 50, "rate": 1450},
			],
		)
		doc = frappe.get_doc("Purchase Order", po.name)
		newest = doc.items[-1]
		frappe.db.set_value("Purchase Order Item", newest.name, "warehouse", CENTRAL)
		frappe.db.commit()
		doc.reload()
		doc.flags.ignore_validate_update_after_submit = True
		doc.save()
		frappe.db.commit()
		self.run_queue()
		patches = [w["body"] for w in self.fake_a.writes if w["method"] == "PATCH"]
		self.assertTrue(patches, "the change reached Koomyo")
		line = patches[-1]["lines"][0]
		self.assertEqual(line["quantity_ordered"], "120")
		self.assertEqual(
			sorted((a["destination_id"], a["quantity"], a.get("branch_id")) for a in line["allocations"]),
			sorted([(CENTRAL_WAREHOUSE, "50", None), (BRANCH, "70", BRANCH)]),
		)
