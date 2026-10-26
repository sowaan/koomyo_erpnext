"""Load Menu and Ingredients: every Koomyo menu item and ingredient becomes an Item up front."""

import frappe

from koomyo_erpnext.sync import catalog
from koomyo_erpnext.tests.fake_koomyo import load_fixture
from koomyo_erpnext.tests.helpers import TENANT_A
from koomyo_erpnext.tests.test_sales import order_event
from koomyo_erpnext.tests.test_stock import KITCHEN, MOZZARELLA, StockTestCase

PIZZA = "a0000000-0000-4000-8000-000000000001"
COLA = "a0000000-0000-4000-8000-000000000002"
CATEGORY = "c0000000-0000-4000-8000-000000000001"


def menu_item(item_id, name, price="900.00", category=CATEGORY, active=True, sizes=None, description=None):
	return {
		"id": item_id,
		"object": "menu_item",
		"name": name,
		"description": description,
		"base_price": price,
		"currency": "PKR",
		"active": active,
		"has_sizes": bool(sizes),
		"category_id": category,
		"category": "Pizza" if category else None,
		"station": "Oven",
		"created_at": "2026-10-01T00:00:00Z",
		"updated_at": "2026-10-01T00:00:00Z",
		"sizes": sizes or [],
		"external_refs": {},
	}


class TestCatalog(StockTestCase):
	def setUp(self):
		super().setUp()
		self.fake_a.lists["/categories"] = [
			{
				"id": CATEGORY,
				"object": "category",
				"name": "Pizza",
				"active": True,
				"sort_order": 1,
				"external_refs": {},
			}
		]
		self.fake_a.lists["/menu-items"] = [
			menu_item(
				PIZZA,
				"Chicken Tikka Supreme",
				sizes=[
					{"id": "s1", "name": "Small", "price": "630.00", "active": True},
					{"id": "s2", "name": "Large", "price": "1200.00", "active": True},
				],
				description="<b>Hot</b> & spicy",
			),
			menu_item(COLA, "Cola", price="150.00", category=None),
		]
		self.fake_a.lists["/ingredients"] = [
			self.fake_a.ingredients[KITCHEN],
			self.fake_a.ingredients[MOZZARELLA],
		]
		self.fake_a.writes.clear()

	def tearDown(self):
		for group in frappe.get_all(
			"Item Group", filters={"name": ("like", f"%({self.conn_a.name})")}, pluck="name"
		):
			frappe.db.delete("Item Group", {"name": group})
		frappe.db.delete("Item Group", {"name": f"{self.conn_a.name} Menu"})
		frappe.db.commit()
		super().tearDown()

	def load(self, preview=False):
		with self.koomyo():
			return catalog.load_catalog(self.conn_a.name, preview=preview)

	def item(self, koomyo_id):
		name = frappe.db.get_value("Item", {"koomyo_connection": self.conn_a.name, "koomyo_id": koomyo_id})
		return frappe.get_doc("Item", name) if name else None

	def test_loads_menu_and_ingredients(self):
		summary = self.load()
		self.assertEqual(summary["menu"]["created"], 2, summary)
		self.assertEqual(summary["ingredients"]["created"], 2, summary)
		self.assertEqual(summary["problems"], [])

		pizza = self.item(PIZZA)
		self.assertEqual(
			(pizza.item_name, pizza.is_stock_item, pizza.has_variants, pizza.item_group),
			("Chicken Tikka Supreme", 0, 1, f"Pizza ({self.conn_a.name})"),
		)
		self.assertEqual(
			frappe.db.get_value("Item Group", pizza.item_group, "parent_item_group"),
			f"{self.conn_a.name} Menu",
		)
		self.assertIn("Small 630.00", pizza.description)
		self.assertIn("Station: Oven", pizza.description)
		self.assertNotIn("<b>Hot</b>", pizza.description, "Koomyo text is escaped, never markup")
		self.assertFalse(
			frappe.db.exists("Item Price", {"item_code": pizza.name}),
			"ERPNext prices variants, not templates",
		)
		small, large = self.item("s1"), self.item("s2")
		self.assertEqual((small.variant_of, large.variant_of), (pizza.name, pizza.name))
		self.assertEqual(
			(small.item_name, small.attributes[0].attribute_value), ("Chicken Tikka Supreme (Small)", "Small")
		)

		def price(code):
			return frappe.db.get_value(
				"Item Price", {"item_code": code, "price_list": "_Koomyo Selling"}, "price_list_rate"
			)

		self.assertEqual((price(small.name), price(large.name)), (630, 1200))
		self.assertEqual(summary["variants"]["created"], 2)
		cola = self.item(COLA)
		self.assertEqual(cola.item_group, "_Koomyo Menu", "no category: the connection's Menu Item Group")
		self.assertEqual(price(cola.name), 150)

		cheese = self.item(MOZZARELLA)
		self.assertEqual((cheese.item_name, cheese.is_stock_item, cheese.stock_uom), ("Mozzarella", 1, "Kg"))
		self.assertEqual(cheese.valuation_rate, 1450)
		refs = sorted((r["entity_type"], r["entity_id"]) for r in self.fake_a.refs)
		self.assertEqual(
			refs,
			sorted(
				[
					("menu_item", PIZZA),
					("menu_item", COLA),
					("ingredient", KITCHEN),
					("ingredient", MOZZARELLA),
				]
			),
		)
		self.assertIn("2 new", frappe.db.get_value("Koomyo Connection", self.conn_a.name, "catalog_summary"))

	def test_loading_twice_changes_nothing_and_follows_koomyo(self):
		self.load()
		before = frappe.db.count("Item", {"koomyo_connection": self.conn_a.name})
		summary = self.load()
		self.assertEqual((summary["menu"]["unchanged"], summary["ingredients"]["unchanged"]), (2, 2))
		self.assertEqual(frappe.db.count("Item", {"koomyo_connection": self.conn_a.name}), before)

		self.fake_a.lists["/menu-items"][1].update({"name": "Cola 500ml", "active": False})
		summary = self.load()
		self.assertEqual(summary["menu"]["updated"], 1)
		cola = self.item(COLA)
		self.assertEqual((cola.item_name, cola.disabled), ("Cola 500ml", 1))

	def test_preview_makes_nothing(self):
		summary = self.load(preview=True)
		self.assertEqual(summary["menu"]["created"], 2)
		self.assertIsNone(self.item(PIZZA))
		self.assertIsNone(self.item(MOZZARELLA))
		self.assertFalse(frappe.db.exists("Item Group", f"{self.conn_a.name} Menu"))
		self.assertEqual(self.fake_a.writes, [])
		self.assertIn(
			"Preview", frappe.db.get_value("Koomyo Connection", self.conn_a.name, "catalog_summary")
		)

	def test_dry_run_connection_loads_items_but_writes_nothing_to_koomyo(self):
		self.conn_a.reload()
		self.conn_a.dry_run = 1
		self.conn_a.save()
		self.load()
		self.assertIsNotNone(self.item(PIZZA))
		self.assertEqual(self.fake_a.writes, [])

	def test_one_bad_record_does_not_stop_the_rest(self):
		self.fake_a.add_ingredient("ing-oil", "Oil", "l")
		self.fake_a.lists["/ingredients"].append(self.fake_a.ingredients["ing-oil"])
		summary = self.load()
		self.assertEqual(summary["ingredients"]["created"], 2)
		self.assertEqual(len(summary["problems"]), 1)
		self.assertIn("Oil", summary["problems"][0])
		self.assertIn("UOM Map", summary["problems"][0])

	def test_a_sale_after_loading_uses_the_loaded_item(self):
		self.load()
		pizza = self.item(PIZZA)
		event = order_event(
			lines=[
				{
					"item_id": PIZZA,
					"name": "Chicken Tikka Supreme",
					"size": "Small",
					"size_id": "s1",
					"quantity": 1,
					"unit_price": "630.00",
					"line_total": "630.00",
				}
			],
			payments=[("cash", "724.50")],
			items_subtotal="630.00",
			tax="94.50",
			total="724.50",
			amount_due="724.50",
			amount_paid="724.50",
		)
		self.assertEqual(self.book(event).status, "Done")
		self.assertEqual(
			self.invoice().items[0].item_code, self.item("s1").name, "booked on the Small variant"
		)
		self.assertEqual(frappe.db.get_value("Item", self.item("s1").name, "variant_of"), pizza.name)
		self.assertEqual(
			frappe.db.count("Item", {"koomyo_connection": self.conn_a.name, "variant_of": pizza.name}), 2
		)

	def test_a_size_removed_in_koomyo_is_disabled(self):
		self.load()
		self.fake_a.lists["/menu-items"][0]["sizes"] = self.fake_a.lists["/menu-items"][0]["sizes"][:1]
		self.load()
		self.assertEqual(self.item("s2").disabled, 1)
		self.assertEqual(self.item("s1").disabled, 0)

	def test_a_plain_item_with_history_keeps_booking_sizes_on_itself(self):
		from koomyo_erpnext.sync.items import get_or_create_item

		get_or_create_item(
			self.conn_a, PIZZA, "Chicken Tikka Supreme", is_stock_item=0, item_group="_Koomyo Menu"
		)
		event = order_event(
			lines=[
				{
					"item_id": PIZZA,
					"name": "Chicken Tikka Supreme",
					"size": None,
					"size_id": None,
					"quantity": 1,
					"unit_price": "630.00",
					"line_total": "630.00",
				}
			],
			payments=[("cash", "724.50")],
			items_subtotal="630.00",
			tax="94.50",
			total="724.50",
			amount_due="724.50",
			amount_paid="724.50",
		)
		self.assertEqual(self.book(event).status, "Done")
		summary = self.load()
		self.assertEqual(summary["problems"], [])
		self.assertEqual(
			self.item(PIZZA).has_variants, 0, "ERPNext cannot turn an Item with invoices into a template"
		)

	def test_button_checks_and_queues(self):
		self.conn_a.reload()
		message = self.conn_a.load_catalog(preview=1)
		self.assertIn("background", message)
		job = [
			kw
			for _a, kw in self.enqueued
			if kw.get("connection_name") == self.conn_a.name and "catalog" in _a[0]
		][-1]
		self.assertTrue(job["preview"])
		self.conn_a.set("uom_map", [])
		self.conn_a.save()
		with self.assertRaises(frappe.ValidationError):
			self.conn_a.load_catalog()
