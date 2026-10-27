"""Slice 4: stock events -> Stock Entries / Stock Reconciliation; transfers -> Material Transfer.

Payloads are the docs examples (fixtures/event_stock_*.json, event_waste_recorded.json,
event_stock_transfer_completed.json) for the ingredient "Kitchen stock" (pcs, 12.50).
"""

import copy
from decimal import Decimal as D

import frappe

from koomyo_erpnext.sync import events
from koomyo_erpnext.tests.fake_koomyo import load_fixture
from koomyo_erpnext.tests.helpers import (
	CENTRAL_WAREHOUSE,
	TENANT_A,
	configure_stock,
	gl,
	refuse_negative_stock,
)
from koomyo_erpnext.tests.test_sales import SalesTestCase

KITCHEN = "10000000-0000-4000-8000-000000000001"
MOZZARELLA = "2bf072f8-6896-44ab-8724-e48946ccc134"
STORES = "Stores - _KTA"
CENTRAL = "Finished Goods - _KTA"


def fixture(name, seq, **data):
	event = load_fixture(name)
	event.update({"tenant_id": TENANT_A, "seq": seq, "id": f"{name}-{seq}"})
	event["data"].update(data)
	return event


def adjustment(
	seq, change, ingredient=KITCHEN, location=None, date="2026-10-10", movement_id=None, unit_cost="12.5000"
):
	event = fixture(
		"event_stock_adjusted",
		seq,
		quantity_change=change,
		business_date=date,
		ingredient_id=ingredient,
		id=movement_id or f"move-{seq}",
		unit_cost=unit_cost,
	)
	if location:
		event["data"]["location"] = location
	return event


class StockTestCase(SalesTestCase):
	def setUp(self):
		super().setUp()
		configure_stock(self.conn_a, "_KTA")
		self.conn_a.reload()
		self.fake_a.add_ingredient(KITCHEN, "Kitchen stock", "pcs", "12.5000")
		self.fake_a.add_ingredient(MOZZARELLA, "Mozzarella", "kg", "1450.0000")

	def item(self, ingredient_id=KITCHEN):
		return frappe.db.get_value(
			"Item", {"koomyo_connection": self.conn_a.name, "koomyo_id": ingredient_id}
		)

	def qty(self, warehouse, ingredient_id=KITCHEN):
		return D(
			str(
				frappe.db.get_value(
					"Bin", {"item_code": self.item(ingredient_id), "warehouse": warehouse}, "actual_qty"
				)
				or 0
			)
		)


class TestStock(StockTestCase):
	def test_docs_day_of_stock(self):
		# +10 by hand, then the docs' usage (7), waste (2, spoilage), adjustment (-1) and count (to 3).
		self.assertEqual(self.book(adjustment(1, "10.0000")).status, "Done")
		item = frappe.get_doc("Item", self.item())
		self.assertEqual(
			(item.item_name, item.stock_uom, item.is_stock_item, item.item_group),
			("Kitchen stock", "Nos", 1, "_Koomyo Ingredients"),
		)
		self.assertEqual(self.qty(STORES), D("10"))

		usage = self.book(fixture("event_stock_usage_daily", 2))
		self.assertEqual(usage.status, "Done", usage.message)
		self.assertEqual(self.qty(STORES), D("3"))
		self.assertEqual(
			gl("Stock Entry", usage.reference_name),
			{"Stock In Hand - _KTA": D("-87.50"), "Cost of Goods Sold - _KTA": D("87.50")},
		)
		self.assertEqual(
			frappe.db.get_value("Stock Entry", usage.reference_name, "koomyo_id"),
			"usage:90000000-0000-4000-8000-000000000001:2026-10-11",
		)

		waste = self.book(fixture("event_waste_recorded", 3))
		self.assertEqual(waste.status, "Done", waste.message)
		self.assertEqual(
			gl("Stock Entry", waste.reference_name),
			{"Stock In Hand - _KTA": D("-25.00"), "Miscellaneous Expenses - _KTA": D("25.00")},
		)

		adjusted = self.book(fixture("event_stock_adjusted", 4))
		self.assertEqual(adjusted.status, "Done", adjusted.message)
		self.assertEqual(self.qty(STORES), D("0"))

		count = self.book(fixture("event_stock_count_closed", 5))
		self.assertEqual(count.status, "Done", count.message)
		self.assertEqual(count.reference_doctype, "Stock Reconciliation")
		self.assertEqual(self.qty(STORES), D("3"))
		self.assertEqual(
			gl("Stock Reconciliation", count.reference_name),
			{"Stock In Hand - _KTA": D("37.50"), "Stock Adjustment - _KTA": D("-37.50")},
		)

	def test_replay_changes_nothing(self):
		self.book(adjustment(1, "10.0000"))
		event = fixture("event_stock_usage_daily", 2)
		self.book(event)
		self.book(copy.deepcopy(event) | {"id": "resent", "seq": 3})
		self.assertEqual(self.qty(STORES), D("3"))
		self.assertEqual(
			len(frappe.get_all("Stock Entry", filters={"koomyo_connection": self.conn_a.name})), 2
		)

	def test_before_opening_stock_date_is_skipped(self):
		log = self.book(adjustment(1, "10.0000", date="2026-09-30"))
		self.assertEqual(log.status, "Skipped")
		self.assertIn("Opening Stock Date", log.message)
		self.assertIsNone(self.item())

	def test_unit_and_waste_reason_need_mapping(self):
		self.fake_a.add_ingredient("ing-l", "Oil", "l")
		log = self.book(adjustment(1, "5.0000", ingredient="ing-l"))
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn("'l'", log.message)
		self.assertIn("UOM Map", log.message)

		self.book(adjustment(2, "10.0000"))
		log = self.book(fixture("event_waste_recorded", 3, reason="comp"))
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn("Waste Map", log.message)

	def test_menu_item_waste_is_not_guessed(self):
		log = self.book(
			fixture(
				"event_waste_recorded",
				1,
				ingredient=None,
				menu_item_id="a0000000-0000-4000-8000-000000000001",
				lines=[],
			)
		)
		self.assertEqual((log.status, log.error_class), ("Failed", "Fatal"))
		self.assertIn("whole menu item", log.message)
		self.assertIn("cancelled order", log.message)

	def test_menu_item_waste_is_booked_from_its_lines(self):
		self.assertEqual(self.book(adjustment(1, "10.0000")).status, "Done")
		self.assertEqual(
			self.book(adjustment(2, "10.0000", ingredient=MOZZARELLA, unit_cost="1450.0000")).status, "Done"
		)
		log = self.book(
			fixture(
				"event_waste_recorded",
				3,
				ingredient=None,
				menu_item_id="a0000000-0000-4000-8000-000000000001",
				quantity="1.000",
				lines=[
					{
						"ingredient_id": KITCHEN,
						"ingredient_name": "Kitchen stock",
						"quantity": "2.000",
						"unit": "pcs",
						"unit_cost": "12.5000",
						"value": "25.00",
					},
					{
						"ingredient_id": MOZZARELLA,
						"ingredient_name": "Mozzarella",
						"quantity": "0.250",
						"unit": "kg",
						"unit_cost": "1450.0000",
						"value": "362.50",
					},
				],
			)
		)
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		rows = frappe.get_all(
			"Stock Entry Detail",
			filters={"parent": log.reference_name},
			fields=["item_code", "qty", "s_warehouse"],
		)
		self.assertEqual(
			{r.item_code: (r.qty, r.s_warehouse) for r in rows},
			{self.item(KITCHEN): (2, STORES), self.item(MOZZARELLA): (0.25, STORES)},
		)
		self.assertEqual(self.qty(STORES, KITCHEN), D("8"))
		self.assertEqual(self.qty(STORES, MOZZARELLA), D("9.75"))
		# the same waste again (a resent event) books nothing more
		again = self.book(
			fixture(
				"event_waste_recorded",
				4,
				ingredient=None,
				menu_item_id="a0000000-0000-4000-8000-000000000001",
				lines=[
					{
						"ingredient_id": KITCHEN,
						"ingredient_name": "Kitchen stock",
						"quantity": "2.000",
						"unit": "pcs",
						"unit_cost": "12.5000",
						"value": "25.00",
					}
				],
			)
		)
		self.assertEqual(again.status, "Done")
		self.assertEqual(self.qty(STORES, KITCHEN), D("8"))

	def test_ingredient_waste_with_no_lines_still_books_from_the_waste_itself(self):
		self.assertEqual(self.book(adjustment(1, "10.0000")).status, "Done")
		log = self.book(fixture("event_waste_recorded", 3, lines=[]))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertEqual(self.qty(STORES, KITCHEN), D("8"))

	def test_a_negative_line_is_refused(self):
		self.assertEqual(self.book(adjustment(1, "10.0000")).status, "Done")
		bad = [
			{
				"ingredient_id": KITCHEN,
				"ingredient_name": "Kitchen stock",
				"quantity": "-2",
				"unit": "pcs",
				"unit_cost": "1",
				"value": "-2",
			}
		]
		log = self.book(fixture("event_waste_recorded", 3, lines=bad))
		self.assertEqual(log.error_class, "Fatal")

	def test_not_enough_stock_in_erpnext_is_refused_clearly(self):
		refuse_negative_stock(self)
		log = self.book(fixture("event_stock_usage_daily", 1))
		self.assertEqual((log.status, log.error_class), ("Failed", "Fatal"))
		self.assertIn("ERPNext refused it", log.message)
		self.assertEqual(frappe.get_all("Stock Entry", filters={"koomyo_connection": self.conn_a.name}), [])

	def test_ingredient_linked_by_external_reference(self):
		existing = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "_KTEST-CHEESE",
				"item_group": "_Koomyo Ingredients",
				"stock_uom": "Kg",
				"is_stock_item": 1,
			}
		).insert(ignore_permissions=True)
		self.fake_a.ingredients[MOZZARELLA]["external_refs"] = {"erpnext": existing.name}
		self.assertEqual(self.book(adjustment(1, "5.0000", ingredient=MOZZARELLA)).status, "Done")
		self.assertEqual(self.item(MOZZARELLA), existing.name)
		self.assertEqual(self.qty(STORES, MOZZARELLA), D("5"))
		frappe.db.delete("Bin", {"item_code": existing.name})

	def test_net_negative_usage_puts_stock_back(self):
		self.book(adjustment(1, "10.0000"))
		event = fixture("event_stock_usage_daily", 2)
		event["data"]["lines"][0].update({"quantity_used": "-2.0000", "cost": "-25.00"})
		log = self.book(event)
		self.assertEqual(log.status, "Done", log.message)
		self.assertEqual(self.qty(STORES), D("12"))


class TestTransfers(StockTestCase):
	def stock_central(self, qty="5.0000"):
		location = {"type": "warehouse", "id": CENTRAL_WAREHOUSE}
		self.assertEqual(
			self.book(
				adjustment(1, qty, ingredient=MOZZARELLA, location=location, unit_cost="1450.0000")
			).status,
			"Done",
		)

	def test_transfer_moves_what_arrived(self):
		self.stock_central()
		event = fixture("event_stock_transfer_completed", 2)
		event["data"]["lines"][0]["quantity_received"] = "5.000"
		log = self.book(event)
		self.assertEqual(log.status, "Done", log.message)
		self.assertEqual(
			frappe.db.get_value("Stock Entry", log.reference_name, "purpose"), "Material Transfer"
		)
		self.assertEqual((self.qty(CENTRAL, MOZZARELLA), self.qty(STORES, MOZZARELLA)), (D("0"), D("5")))

	def test_short_transfer_books_the_loss(self):
		self.stock_central()
		event = fixture("event_stock_transfer_completed", 2)
		event["data"]["lines"][0]["quantity_received"] = "4.000"
		self.assertEqual(self.book(event).status, "Done")
		self.assertEqual((self.qty(CENTRAL, MOZZARELLA), self.qty(STORES, MOZZARELLA)), (D("0"), D("4")))
		loss = frappe.db.get_value(
			"Stock Entry", {"koomyo_connection": self.conn_a.name, "koomyo_id": ("like", "%:shortfall")}
		)
		self.assertEqual(gl("Stock Entry", loss)["Miscellaneous Expenses - _KTA"], D("1450.00"))

	def test_docs_example_nothing_received(self):
		# The docs example is 'received' with quantity_received 0 of 5 sent.
		self.stock_central()
		log = self.book(fixture("event_stock_transfer_completed", 2))
		self.assertEqual(log.status, "Done", log.message)
		self.assertEqual((self.qty(CENTRAL, MOZZARELLA), self.qty(STORES, MOZZARELLA)), (D("0"), D("0")))

	def test_unmapped_central_warehouse(self):
		self.conn_a.reload()
		self.conn_a.set("branch_map", [r for r in self.conn_a.branch_map if r.location_type != "Warehouse"])
		self.conn_a.save()
		log = self.book(fixture("event_stock_transfer_completed", 2))
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn(CENTRAL_WAREHOUSE, log.message)
		self.assertIn("Location Type 'Warehouse'", log.message)

	def test_transfer_replay(self):
		self.stock_central("10.0000")
		event = fixture("event_stock_transfer_completed", 2)
		event["data"]["lines"][0]["quantity_received"] = "5.000"
		self.book(event)
		self.book(copy.deepcopy(event) | {"id": "resent", "seq": 3})
		self.assertEqual(self.qty(STORES, MOZZARELLA), D("5"))
