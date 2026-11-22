"""menu_item.* and ingredient.* events keep the Items in step with Koomyo (sync/catalog_events.py)."""

import copy

import frappe

from koomyo_erpnext.tests.test_sales import SalesTestCase

MENU_ID = "7a1d2c3b-0000-4000-8000-0000000000a1"
SIZE_S = "7a1d2c3b-0000-4000-8000-0000000000b1"
SIZE_L = "7a1d2c3b-0000-4000-8000-0000000000b2"


def menu_record(**changes):
	record = {
		"object": "menu_item",
		"id": MENU_ID,
		"name": "Zinger Burger",
		"description": "Crispy",
		"category": "Burgers",
		"category_id": "cat-1",
		"active": True,
		"has_sizes": False,
		"sizes": [],
		"base_price": "450.00",
		"station": "grill",
		"track_stock": True,
		"currency": "PKR",
		"external_refs": {},
		"created_at": "2026-11-15T08:00:00Z",
		"updated_at": "2026-11-15T08:00:00Z",
	}
	record.update(changes)
	return record


class TestMenuEvents(SalesTestCase):
	def event(self, kind, record, seq):
		return {
			"id": f"evt-menu-{seq}",
			"object": "event",
			"type": f"menu_item.{kind}",
			"seq": seq,
			"tenant_id": self.conn_a.tenant_id,
			"created": "2026-11-15T08:00:00Z",
			"entity": {"type": "menu_item", "id": record["id"]},
			"data": copy.deepcopy(record),
		}

	def item(self, koomyo_id=MENU_ID):
		name = frappe.db.get_value("Item", {"koomyo_connection": self.conn_a.name, "koomyo_id": koomyo_id})
		return frappe.get_doc("Item", name) if name else None

	def test_a_new_menu_item_makes_an_item_and_an_update_follows_it(self):
		log = self.book(self.event("created", menu_record(), 900))
		self.assertEqual(log.status, "Done", (log.message, (log.error or "")[-300:]))
		item = self.item()
		self.assertEqual((item.item_name, item.is_stock_item, item.disabled), ("Zinger Burger", 0, 0))
		self.assertEqual(log.reference_name, item.name)
		self.assertTrue(
			item.item_group.startswith("Burgers"), item.item_group
		)  # one group per Koomyo category
		renamed = menu_record(name="Zinger Burger Deluxe", active=False)
		self.assertEqual(self.book(self.event("updated", renamed, 901)).status, "Done")
		item = self.item()
		self.assertEqual((item.item_name, item.disabled), ("Zinger Burger Deluxe", 1))
		# the same event again, or the same item again, changes nothing
		self.book(self.event("updated", renamed, 902))
		self.assertEqual(
			frappe.db.count("Item", {"koomyo_connection": self.conn_a.name, "koomyo_id": MENU_ID}), 1
		)

	def test_a_menu_item_with_sizes_is_a_template_with_one_variant_per_size(self):
		record = menu_record(
			has_sizes=True,
			sizes=[
				{"id": SIZE_S, "name": "Small", "price": "300.00", "active": True},
				{"id": SIZE_L, "name": "Large", "price": "600.00", "active": True},
			],
		)
		self.assertEqual(self.book(self.event("created", record, 910)).status, "Done")
		template = self.item()
		self.assertEqual(template.has_variants, 1)
		self.assertEqual(
			sorted(frappe.get_all("Item", filters={"variant_of": template.name}, pluck="item_name")),
			["Zinger Burger (Large)", "Zinger Burger (Small)"],
		)

	def test_a_deleted_menu_item_is_disabled_with_its_sizes_never_deleted(self):
		record = menu_record(
			has_sizes=True, sizes=[{"id": SIZE_S, "name": "Small", "price": "300.00", "active": True}]
		)
		self.book(self.event("created", record, 920))
		template = self.item()
		log = self.book(self.event("deleted", record, 921))
		self.assertEqual(log.status, "Done", log.message)
		self.assertEqual(self.item().disabled, 1)
		variants = frappe.get_all("Item", filters={"variant_of": template.name}, fields=["disabled"])
		self.assertEqual([v.disabled for v in variants], [1])
		self.assertTrue(frappe.db.exists("Item", template.name), "never deleted")
		# a repeat changes nothing; a menu item that was never an Item is skipped with a reason
		self.assertEqual(self.book(self.event("deleted", record, 922)).status, "Done")
		other = menu_record(id="7a1d2c3b-0000-4000-8000-0000000000ff", name="Never seen")
		skipped = self.book(self.event("deleted", other, 923))
		self.assertEqual(skipped.status, "Skipped")
		self.assertIn("nothing to disable", skipped.message)

	def test_when_koomyo_removes_the_sizes_the_size_variants_are_disabled_and_sales_still_book(self):
		"""Half Broast: sizes taken away in Koomyo. ERPNext cannot turn an Item with variants back into a plain
		Item, so it stays the template; its size variants are disabled and a sale without a size books on the
		'No size' variant."""
		sized = menu_record(
			has_sizes=True,
			sizes=[
				{"id": SIZE_S, "name": "Small", "price": "300.00", "active": True},
				{"id": SIZE_L, "name": "Large", "price": "600.00", "active": True},
			],
		)
		self.assertEqual(self.book(self.event("created", sized, 950)).status, "Done")
		template = self.item()
		self.assertEqual(template.has_variants, 1)
		no_sizes = menu_record(has_sizes=False, sizes=[])
		log = self.book(self.event("updated", no_sizes, 951))
		self.assertEqual(log.status, "Done", (log.message, (log.error or "")[-300:]))
		variants = frappe.get_all(
			"Item", filters={"variant_of": template.name}, fields=["item_name", "disabled"]
		)
		self.assertEqual(
			{v.item_name: v.disabled for v in variants},
			{"Zinger Burger (Small)": 1, "Zinger Burger (Large)": 1},
		)
		self.assertEqual(self.item().has_variants, 1, "ERPNext keeps the template")
		# a sale without a size now books (on the No size variant), and an update again changes nothing
		from koomyo_erpnext.sync.items import menu_item

		code = menu_item(
			self.conn_a, {"item_id": MENU_ID, "name": "Zinger Burger", "size_id": None, "size": None}
		)
		self.assertEqual(frappe.db.get_value("Item", code, "item_name"), "Zinger Burger (No size)")
		self.assertEqual(frappe.db.get_value("Item", code, "disabled"), 0)
		self.assertEqual(self.book(self.event("updated", no_sizes, 952)).status, "Done")
		self.assertEqual(frappe.db.get_value("Item", code, "disabled"), 0)

	def test_a_menu_event_with_the_sales_sync_off_is_not_booked(self):
		self.conn_a.reload()
		self.conn_a.sync_sales = 0
		self.conn_a.save()
		log = self.book(self.event("created", menu_record(), 930))
		self.assertEqual(log.status, "Skipped")
		self.assertIsNone(self.item())

	def test_something_that_is_not_a_menu_item_is_refused(self):
		bad = self.event("created", menu_record(object="ingredient"), 940)
		log = self.book(bad)
		self.assertEqual((log.status, log.error_class), ("Failed", "Fatal"))
