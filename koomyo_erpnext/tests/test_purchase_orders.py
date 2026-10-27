"""Section 2: Koomyo purchase orders (created, updated, cancelled, received) and their tax and charges.

Samples start from the docs examples (fixtures/event_purchase_*.json). Money is compared as exact
Decimals; stock value is read from the Stock Ledger.
"""

import copy
from decimal import Decimal as D

import frappe
from frappe.utils import flt

from koomyo_erpnext.sync import events, purchase_orders
from koomyo_erpnext.tests.fake_koomyo import load_fixture
from koomyo_erpnext.tests.helpers import COMPANY_A, TENANT_A, ensure_account, hard_delete
from koomyo_erpnext.tests.test_purchasing import PurchasingTestCase
from koomyo_erpnext.tests.test_stock import MOZZARELLA

BRANCH = "90000000-0000-4000-8000-000000000001"
SUPPLIER = {"id": "koomyo-supplier-1", "name": "Koomyo Cheese Co"}
ORDER_ID = "26711726-8300-4adc-a447-2a6213d914fa"
LINE_ID = "a93db756-ed1d-4386-9fb9-fbb95c7b9aca"


def make_order(status="ordered", updated_at="2026-10-12T17:40:02Z", **changes):
	"""A Koomyo purchase order the way the docs show it: 10 kg at 1450.00, no tax."""
	order = copy.deepcopy(load_fixture("event_purchase_created")["data"])
	order.update(
		{
			"id": ORDER_ID,
			"status": status,
			"updated_at": updated_at,
			"external_refs": {},
			"supplier": dict(SUPPLIER),
			"branch_id": BRANCH,
			"receipts": [],
		}
	)
	order["lines"][0].update({"id": LINE_ID, "ingredient_id": MOZZARELLA})
	order.update(changes)
	return order


def event_for(order, event_type, seq, eid=None):
	event = copy.deepcopy(load_fixture("event_purchase_created"))
	event.update(
		{
			"id": eid or f"evt-po-{seq}",
			"seq": seq,
			"type": event_type,
			"tenant_id": TENANT_A,
			"data": copy.deepcopy(order),
			"entity": {"type": "purchase_order", "id": order["id"]},
		}
	)
	return event


def receipt(rid, qty, at="2026-10-13T09:00:00Z", **extra):
	return {
		"id": rid,
		"received_at": at,
		"received_by": "Sam",
		"delivery_note": f"DN-{rid}",
		"closed_short": False,
		"lines": [{"line_id": LINE_ID, "ingredient_id": MOZZARELLA, "quantity": qty}],
		**extra,
	}


class PurchaseOrderCase(PurchasingTestCase):
	def setUp(self):
		super().setUp()
		self.conn_a.reload()
		self.conn_a.update(
			{
				"book_koomyo_purchases": 1,
				"main_warehouse": "Stores - _KTA",
				"supplier_group": "_Koomyo Suppliers",
			}
		)
		self.conn_a.save()
		self.po_seq = 200
		self.opening_qty = self.qty("Stores - _KTA", MOZZARELLA)

	def tearDown(self):
		for name in frappe.get_all("Landed Cost Voucher", filters={"company": COMPANY_A}, pluck="name"):
			for table in ("GL Entry", "Stock Ledger Entry", "Payment Ledger Entry"):
				frappe.db.delete(table, {"voucher_type": "Landed Cost Voucher", "voucher_no": name})
			hard_delete("Landed Cost Voucher", name)
		super().tearDown()

	def send(self, order, event_type="purchase.updated", **kw):
		self.po_seq += 1
		return self.book(event_for(order, event_type, self.po_seq, **kw))

	def po(self, **filters):
		name = frappe.db.get_value(
			"Purchase Order", {"koomyo_connection": self.conn_a.name, "koomyo_id": ORDER_ID, **filters}
		)
		return frappe.get_doc("Purchase Order", name) if name else None

	def receipts(self):
		return frappe.get_all(
			"Purchase Receipt",
			filters={"koomyo_connection": self.conn_a.name, "docstatus": 1},
			fields=["name", "koomyo_id"],
			order_by="creation",
		)

	def accounts(self):
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


class TestPurchaseOrderEvents(PurchaseOrderCase):
	def test_draft_creates_nothing_then_ordered_creates_the_order_once(self):
		draft = make_order("draft", "2026-10-12T17:26:12Z")
		log = self.send(draft, "purchase.created")
		self.assertEqual(log.status, "Skipped")
		self.assertIn("draft", log.message)
		self.assertIsNone(self.po())
		self.assertEqual(frappe.get_all("Supplier", filters={"koomyo_id": SUPPLIER["id"]}), [])

		ordered = make_order("ordered")
		log = self.send(ordered)
		self.assertEqual(log.status, "Done", (log.message, log.error[-500:]))
		po = self.po()
		self.assertEqual((po.docstatus, po.set_warehouse), (1, "Stores - _KTA"))
		self.assertEqual((flt(po.items[0].qty, 3), flt(po.items[0].rate, 2)), (10, 1450))
		self.assertEqual(po.koomyo_updated_at, "2026-10-12T17:40:02Z")
		self.assertEqual(D(str(po.grand_total)).quantize(D("0.01")), D("14500.00"))

		again = self.send(ordered, eid="evt-resent")
		self.assertEqual(again.status, "Done")
		self.assertEqual(
			len(frappe.get_all("Purchase Order", filters={"koomyo_connection": self.conn_a.name})), 1
		)

	def test_older_copy_arriving_late_changes_nothing(self):
		self.send(make_order("ordered", "2026-10-12T17:40:02Z"))
		po = self.po()
		late = make_order("ordered", "2026-10-12T17:30:00Z")
		late["lines"][0].update({"quantity_ordered": "99.000", "line_total": "143550.00"})
		log = self.send(late)
		self.assertEqual(log.status, "Done")
		self.assertEqual(flt(self.po().items[0].qty, 3), 10)
		self.assertEqual(self.po().name, po.name)

	def test_an_order_for_a_central_warehouse_goes_to_its_mapped_warehouse(self):
		central = "61111d66-8b23-4992-a9e7-e1508eb02751"
		self.conn_a.reload()
		self.conn_a.append(
			"branch_map",
			{"location_type": "Warehouse", "koomyo_branch_id": central, "warehouse": "Finished Goods - _KTA"},
		)
		self.conn_a.save()
		log = self.send(make_order("ordered", branch_id=None, warehouse_id=central))
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		self.assertEqual(self.po().set_warehouse, "Finished Goods - _KTA")
		unmapped = make_order("ordered", branch_id=None, warehouse_id="nowhere", id="po-2")
		bad = self.send(unmapped, eid="evt-bad")
		self.assertEqual(bad.error_class, "NeedsMapping")
		self.assertIn("nowhere", bad.message)

	def test_edit_before_any_delivery_rebuilds_the_order(self):
		self.send(make_order("ordered", "2026-10-12T17:40:02Z"))
		old = self.po()
		edited = make_order("ordered", "2026-10-12T18:00:00Z")
		edited["lines"][0].update(
			{"quantity_ordered": "12.000", "ordered_value": "17400.00", "line_total": "17400.00"}
		)
		edited["total_ordered"] = "17400.00"
		edited["items_without_tax"] = "17400.00"
		log = self.send(edited, "purchase.updated")
		self.assertEqual(log.status, "Done", (log.message, log.error[-500:]))
		new = self.po()
		self.assertNotEqual(new.name, old.name)
		self.assertEqual(flt(new.items[0].qty, 3), 12)
		self.assertEqual(frappe.db.get_value("Purchase Order", old.name, "docstatus"), 2)
		self.assertFalse(frappe.db.get_value("Purchase Order", old.name, "koomyo_id"))

	def test_a_send_by_email_does_not_rebuild(self):
		self.send(make_order("ordered", "2026-10-12T17:40:02Z"))
		old = self.po()
		sent = make_order("ordered", "2026-10-12T19:00:00Z", sent_at="2026-10-12T19:00:00Z")
		self.assertEqual(self.send(sent).status, "Done")
		self.assertEqual(self.po().name, old.name)
		self.assertEqual(self.po().koomyo_updated_at, "2026-10-12T19:00:00Z")

	def test_cancel_cancels_the_counterpart(self):
		self.send(make_order("ordered"))
		cancelled = make_order(
			"cancelled",
			"2026-10-12T18:05:30Z",
			cancelled_at="2026-10-12T18:05:30Z",
			cancel_reason="Wrong supplier",
		)
		log = self.send(cancelled, "purchase.cancelled")
		self.assertEqual(log.status, "Done", log.message)
		po = frappe.get_doc("Purchase Order", log.reference_name)
		self.assertEqual(po.docstatus, 2)

	def test_cancel_with_goods_already_received_is_flagged_not_forced(self):
		self.send(make_order("ordered"))
		self.send(
			make_order("received", "2026-10-13T09:00:00Z", receipts=[receipt("rcpt-1", "10.000")]),
			"purchase.received",
		)
		cancelled = make_order("cancelled", "2026-10-14T09:00:00Z", cancel_reason="Oops")
		log = self.send(cancelled, "purchase.cancelled")
		self.assertEqual(log.status, "Failed")
		self.assertIn("already received", log.message)
		self.assertEqual(self.po().docstatus, 1)

	def test_cancel_for_an_order_we_never_booked_is_skipped(self):
		log = self.send(make_order("cancelled", "2026-10-12T18:05:30Z"), "purchase.cancelled")
		self.assertEqual(log.status, "Skipped")


class TestDeliveries(PurchaseOrderCase):
	def test_each_delivery_is_its_own_receipt(self):
		self.send(make_order("ordered"))
		first = make_order(
			"partial", "2026-10-13T09:00:00Z", receipts=[receipt("rcpt-1", "6.000")], fully_received=False
		)
		log = self.send(first, "purchase.received")
		self.assertEqual(log.status, "Done", (log.message, log.error[-500:]))
		self.assertEqual([r.koomyo_id for r in self.receipts()], ["rcpt-1"])
		self.assertEqual(self.qty("Stores - _KTA", MOZZARELLA), D("6") + self.opening_qty)

		second = make_order(
			"received",
			"2026-10-14T09:00:00Z",
			receipts=[receipt("rcpt-1", "6.000"), receipt("rcpt-2", "4.000", at="2026-10-14T09:00:00Z")],
			fully_received=True,
		)
		log = self.send(second, "purchase.received")
		self.assertEqual(log.status, "Done", (log.message, log.error[-500:]))
		self.assertEqual([r.koomyo_id for r in self.receipts()], ["rcpt-1", "rcpt-2"])
		self.assertEqual(self.qty("Stores - _KTA", MOZZARELLA), D("10") + self.opening_qty)
		self.assertEqual(frappe.db.get_value("Purchase Order", self.po().name, "per_received"), 100)

		# a replay, or the same delivery again in a later copy, books nothing more
		self.send(second, "purchase.received", eid="again")
		self.assertEqual(len(self.receipts()), 2)

	def test_receipt_date_is_the_restaurants_day(self):
		self.send(make_order("ordered"))
		# 21:30Z is already the next day in Asia/Karachi
		self.send(
			make_order(
				"received",
				"2026-10-13T21:30:00Z",
				receipts=[receipt("rcpt-1", "10.000", at="2026-10-13T21:30:00Z")],
			),
			"purchase.received",
		)
		(row,) = self.receipts()
		self.assertEqual(str(frappe.db.get_value("Purchase Receipt", row.name, "posting_date")), "2026-10-14")

	def test_delivery_note_is_kept(self):
		self.send(make_order("ordered"))
		self.send(make_order("received", receipts=[receipt("rcpt-1", "10.000")]), "purchase.received")
		(row,) = self.receipts()
		self.assertEqual(
			frappe.db.get_value("Purchase Receipt", row.name, "supplier_delivery_note"), "DN-rcpt-1"
		)

	def test_order_received_before_deliveries_were_kept_is_one_receipt_by_order_id(self):
		order = make_order("received", receipts=[])
		order["lines"][0]["quantity_received"] = "10.000"
		log = self.send(order, "purchase.received")
		self.assertEqual(log.status, "Done", (log.message, log.error[-500:]))
		self.assertEqual([r.koomyo_id for r in self.receipts()], [ORDER_ID])
		self.send(order, "purchase.received", eid="again")
		self.assertEqual(len(self.receipts()), 1)

	def test_an_order_made_in_erpnext_is_receipted_not_rebuilt(self):
		po = self.make_po(qty=10)
		self.run_queue()
		order = make_order(
			"received",
			"2026-10-13T09:00:00Z",
			external_refs={"erpnext": po.name},
			receipts=[receipt("rcpt-9", "10.000")],
		)
		edited = copy.deepcopy(order)
		edited["lines"][0]["quantity_ordered"] = "99.000"
		log = self.send(edited, "purchase.received")
		self.assertEqual(log.status, "Done", (log.message, log.error[-500:]))
		self.assertEqual(frappe.get_doc("Purchase Order", po.name).docstatus, 1)
		self.assertEqual(flt(frappe.get_doc("Purchase Order", po.name).items[0].qty, 3), 10)
		pr = frappe.get_doc("Purchase Receipt", log.reference_name)
		self.assertEqual((pr.koomyo_id, pr.items[0].purchase_order), ("rcpt-9", po.name))

	def test_cancel_of_an_erpnext_order_follows(self):
		po = self.make_po(qty=10)
		self.run_queue()
		cancelled = make_order(
			"cancelled", external_refs={"erpnext": po.name}, cancel_reason="No longer needed"
		)
		log = self.send(cancelled, "purchase.cancelled")
		self.assertEqual(log.status, "Done", log.message)
		self.assertEqual(frappe.db.get_value("Purchase Order", po.name, "docstatus"), 2)


class TestSafetyNet(PurchaseOrderCase):
	def test_updated_since_runs_the_same_path_and_keeps_its_place(self):
		self.fake_a.lists["/purchase-orders"] = [
			make_order("draft", "2026-10-12T10:00:00Z", id="draft-1"),
			make_order("ordered", "2026-10-12T17:40:02Z"),
		]
		with self.koomyo():
			summary = purchase_orders.sync_purchase_orders(self.conn_a.name)
			events.process_pending(self.conn_a.name)
		self.assertEqual(summary, {"read": 2, "queued": 1})
		self.assertIsNotNone(self.po(), "the ordered one is booked")
		self.assertIsNone(
			frappe.db.get_value("Purchase Order", {"koomyo_id": "draft-1"}), "a draft is never booked"
		)
		self.conn_a.reload()
		self.assertEqual(self.conn_a.purchase_sync_cursor, "2026-10-12T17:40:02Z")
		(call,) = [c for c in self.fake_a.calls if c["path"] == "/purchase-orders"]
		self.assertIn("updated_since", call["params"])

		# a second read finds the same order again and changes nothing
		with self.koomyo():
			again = purchase_orders.sync_purchase_orders(self.conn_a.name)
			events.process_pending(self.conn_a.name)
		self.assertEqual(again["queued"], 0)
		self.assertEqual(
			len(frappe.get_all("Purchase Order", filters={"koomyo_connection": self.conn_a.name})), 1
		)

	def test_a_missed_delivery_is_caught_up(self):
		self.send(make_order("ordered"))
		self.fake_a.lists["/purchase-orders"] = [
			make_order("received", "2026-10-13T09:00:00Z", receipts=[receipt("rcpt-1", "10.000")])
		]
		with self.koomyo():
			purchase_orders.sync_purchase_orders(self.conn_a.name)
			events.process_pending(self.conn_a.name)
		self.assertEqual([r.koomyo_id for r in self.receipts()], ["rcpt-1"])


class TestTaxAndCharges(PurchaseOrderCase):
	def taxed(self, **changes):
		"""10 kg at 1450.00 = 14500.00 with 18% tax added on top: 2610.00, total 17110.00."""
		order = make_order(
			"ordered",
			tax_name="Sales tax",
			tax_rate="18",
			tax_mode="exclusive",
			tax_recoverable=True,
			tax_total="2610.00",
			tax_added="2610.00",
			tax_included="0.00",
			tax_lines=[
				{
					"name": "Sales tax",
					"rate": "18",
					"base": "14500.00",
					"tax": "2610.00",
					"recoverable": True,
					"included_in_prices": False,
				}
			],
			total_ordered="17110.00",
		)
		order["lines"][0].update({"tax_rate": "18", "tax_amount": "2610.00", "line_total": "17110.00"})
		order.update(changes)
		return order

	def test_recoverable_tax_is_input_tax_not_cost(self):
		self.accounts()
		log = self.send(self.taxed())
		self.assertEqual(log.status, "Done", (log.message, log.error[-500:]))
		po = self.po()
		self.assertEqual(D(str(po.grand_total)).quantize(D("0.01")), D("17110.00"))
		(row,) = po.taxes
		self.assertEqual(
			(row.account_head, row.category, row.add_deduct_tax), ("Input Tax - _KTA", "Total", "Add")
		)
		self.assertEqual(D(str(row.tax_amount)).quantize(D("0.01")), D("2610.00"))
		self.assertEqual(flt(po.items[0].rate, 2), 1450)

	def test_tax_that_cannot_be_claimed_is_part_of_cost(self):
		self.accounts()
		order = self.taxed(tax_recoverable=False, non_recoverable_tax="2610.00")
		order["tax_lines"][0]["recoverable"] = False
		self.send(order)
		log = self.send(
			make_order(
				"received",
				"2026-10-13T09:00:00Z",
				receipts=[receipt("rcpt-1", "10.000")],
				**{
					k: order[k]
					for k in (
						"tax_lines",
						"tax_total",
						"tax_added",
						"non_recoverable_tax",
						"total_ordered",
						"tax_recoverable",
					)
				},
				lines=order["lines"],
			),
			"purchase.received",
		)
		self.assertEqual(log.status, "Done", (log.message, log.error[-500:]))
		pr = frappe.get_doc("Purchase Receipt", log.reference_name)
		(row,) = pr.taxes
		self.assertEqual(
			(row.category, row.account_head), ("Valuation and Total", "Landed Cost Clearing - _KTA")
		)
		self.assertEqual(D(str(row.tax_amount)).quantize(D("0.01")), D("2610.00"))
		# 14500 + 2610 spread over 10 kg
		self.assertEqual(flt(pr.items[0].valuation_rate, 2), 1711)

	def test_an_order_without_a_branch_still_gets_a_cost_center_on_its_tax_rows(self):
		self.accounts()
		order = self.taxed(tax_recoverable=False, non_recoverable_tax="2610.00", branch_id=None)
		order["tax_lines"][0]["recoverable"] = False
		self.send(order)
		order.update(
			status="received", updated_at="2026-10-13T09:00:00Z", receipts=[receipt("rcpt-1", "10.000")]
		)
		log = self.send(order, "purchase.received")
		self.assertEqual(log.status, "Done", (log.message, log.error[-500:]))
		pr = frappe.get_doc("Purchase Receipt", log.reference_name)
		self.assertTrue(pr.taxes[0].cost_center)

	def test_withholding_is_a_deduction_never_cost(self):
		self.accounts()
		order = self.taxed(
			withholding_total="-171.10",
			charges=[
				{
					"id": "w1",
					"label": "Withholding",
					"kind": "percent",
					"value": "1",
					"direction": "deduct",
					"tax_percent": "0",
					"spread_over_cost": "none",
					"role": "withholding",
					"taken_of": "total",
					"amount": "-171.10",
					"tax_amount": "0.00",
				}
			],
			total_ordered="16938.90",
		)
		log = self.send(order)
		self.assertEqual(log.status, "Done", (log.message, log.error[-500:]))
		po = self.po()
		self.assertEqual(D(str(po.grand_total)).quantize(D("0.01")), D("16938.90"))
		rows = {r.account_head: r for r in po.taxes}
		self.assertEqual(rows["Withholding Payable - _KTA"].add_deduct_tax, "Deduct")
		self.assertEqual(rows["Withholding Payable - _KTA"].category, "Total")

	def charged(self, spread, amount="1000.00"):
		order = make_order(
			"ordered",
			charges=[
				{
					"id": "c1",
					"label": "Delivery",
					"kind": "amount",
					"value": amount,
					"direction": "add",
					"tax_percent": "0",
					"spread_over_cost": spread,
					"role": "charge",
					"taken_of": "items",
					"amount": amount,
					"tax_amount": "0.00",
				}
			],
			total_ordered=str(D("14500.00") + D(amount)),
		)
		return order

	def test_charge_spread_by_value_lands_in_cost(self):
		self.accounts()
		order = self.charged("value")
		self.send(order)
		order.update(
			status="received", updated_at="2026-10-13T09:00:00Z", receipts=[receipt("rcpt-1", "10.000")]
		)
		log = self.send(order, "purchase.received")
		self.assertEqual(log.status, "Done", (log.message, log.error[-500:]))
		pr = frappe.get_doc("Purchase Receipt", log.reference_name)
		self.assertEqual(flt(pr.items[0].valuation_rate, 2), 1550)

	def test_charge_that_is_only_an_expense_stays_out_of_cost(self):
		self.accounts()
		order = self.charged("none")
		self.send(order)
		self.assertEqual(D(str(self.po().grand_total)).quantize(D("0.01")), D("15500.00"))
		self.assertEqual(self.po().taxes[0].account_head, "Purchase Charges - _KTA")
		order.update(
			status="received", updated_at="2026-10-13T09:00:00Z", receipts=[receipt("rcpt-1", "10.000")]
		)
		log = self.send(order, "purchase.received")
		pr = frappe.get_doc("Purchase Receipt", log.reference_name)
		self.assertEqual(flt(pr.items[0].valuation_rate, 2), 1450)

	def test_charge_spread_by_quantity_is_a_landed_cost_voucher_per_delivery(self):
		self.accounts()
		order = self.charged("quantity", "1000.00")
		self.send(order)
		one = dict(
			order, status="partial", updated_at="2026-10-13T09:00:00Z", receipts=[receipt("rcpt-1", "4.000")]
		)
		self.assertEqual(self.send(one, "purchase.received").status, "Done")
		two = dict(
			order,
			status="received",
			updated_at="2026-10-14T09:00:00Z",
			receipts=[receipt("rcpt-1", "4.000"), receipt("rcpt-2", "6.000", at="2026-10-14T09:00:00Z")],
		)
		log = self.send(two, "purchase.received")
		self.assertEqual(log.status, "Done", (log.message, log.error[-500:]))
		amounts = sorted(
			D(str(a)).quantize(D("0.01"))
			for a in frappe.get_all(
				"Landed Cost Taxes and Charges",
				filters={
					"parent": (
						"in",
						frappe.get_all(
							"Landed Cost Voucher",
							filters={"koomyo_connection": self.conn_a.name},
							pluck="name",
						),
					)
				},
				pluck="amount",
			)
		)
		self.assertEqual(amounts, [D("400.00"), D("600.00")], "40% then the remainder, so the total is exact")
		self.assertEqual(sum(amounts), D("1000.00"))

	def test_override_turns_every_charge_into_an_expense(self):
		self.accounts()
		self.conn_a.landed_cost_method = "None"
		self.conn_a.save()
		order = self.charged("value")
		self.send(order)
		self.assertEqual(self.po().taxes[0].category, "Total")
		self.assertEqual(self.po().taxes[0].account_head, "Purchase Charges - _KTA")

	def test_missing_account_names_the_setting(self):
		self.conn_a.reload()
		self.conn_a.purchase_tax_account = None
		self.conn_a.save()
		log = self.send(self.taxed())
		self.assertEqual(log.status, "Failed")
		self.assertIn("Purchase Tax Account", log.message)
		self.assertIsNone(self.po())
