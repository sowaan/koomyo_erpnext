"""Item 8: ERPNext Items and Warehouses -> Koomyo ingredients and warehouses (scope ingredients.write).

Opt in on the connection and per Item or Warehouse. Only what differs from Koomyo is patched, so what the
menu load just wrote sends nothing back.
"""

import frappe

from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry
from koomyo_erpnext.sync import events
from koomyo_erpnext.tests.test_stock import KITCHEN, STORES, StockTestCase, adjustment

TAG = "_KT "


class IngredientsOutCase(StockTestCase):
	def setUp(self):
		super().setUp()
		self.conn_a.reload()
		self.conn_a.push_ingredients = 1
		self.conn_a.save()
		self.assertEqual(self.book(adjustment(1, "5.0000", unit_cost="12.5000")).status, "Done")
		self.fake_a.writes.clear()
		self.fake_a.stock_calls.clear()

	def tearDown(self):
		for name in frappe.get_all("Item", filters={"item_code": ("like", f"{TAG}%")}, pluck="name"):
			frappe.db.delete("Item Reorder", {"parent": name})
			frappe.db.delete("Item Default", {"parent": name})
			frappe.delete_doc("Item", name, force=True, ignore_permissions=True)
		for name in frappe.get_all(
			"Warehouse", filters={"warehouse_name": ("like", f"{TAG}%")}, pluck="name"
		):
			frappe.delete_doc("Warehouse", name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def run_queue(self):
		with self.koomyo():
			events.process_pending(self.conn_a.name)

	def logs_of(self, *types):
		return [
			l
			for l in self.logs(self.conn_a.name, received_via="ERPNext")
			if not types or l.event_type in types
		]

	def calls(self, path_start):
		return [(p, b) for p, b in self.fake_a.stock_calls if p.startswith(path_start)]

	def new_item(self, name="Bag", uom="Nos", send_as="Food", cost=40, **extra):
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"{TAG}{name}",
				"item_name": f"{TAG}{name}",
				"item_group": "_Koomyo Ingredients",
				"stock_uom": uom,
				"is_stock_item": 1,
				"valuation_rate": cost,
				"koomyo_send_as": send_as,
				**extra,
			}
		)
		item.insert()
		frappe.db.commit()
		self.run_queue()
		return item

	def save(self, item):
		item.save()
		frappe.db.commit()
		self.run_queue()
		return item

	def linked_item(self):
		"""An Item that is already a Koomyo ingredient (made by sending it)."""
		item = self.new_item("Linked", uom="Nos")
		self.fake_a.stock_calls.clear()
		self.fake_a.writes.clear()
		return frappe.get_doc("Item", item.name)


class TestCreate(IngredientsOutCase):
	def test_a_marked_item_becomes_an_ingredient_and_carries_its_id(self):
		item = self.new_item("Bag", uom="Nos", cost=40)
		(log,) = self.logs_of("erpnext.ingredient_create")
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		((_path, body),) = self.calls("/ingredients")
		self.assertEqual(
			(body["name"], body["unit"], body["kind"], body["cost_per_unit"], body["active"]),
			(f"{TAG}Bag", "pcs", "food", "40", True),
		)
		self.assertEqual(body["external_ref"], {"system": "erpnext", "external_id": item.name})
		row = frappe.db.get_value(
			"Item", item.name, ["koomyo_connection", "koomyo_id", "koomyo_kind"], as_dict=True
		)
		self.assertEqual((row.koomyo_connection, row.koomyo_kind), (self.conn_a.name, "food"))
		self.assertTrue(row.koomyo_id in self.fake_a.ingredients)

	def test_a_replay_makes_no_second_ingredient(self):
		self.new_item("Bag")
		(log,) = self.logs_of("erpnext.ingredient_create")
		retry([log.name])
		self.run_queue()
		self.assertEqual(len(self.calls("/ingredients")), 1)

	def test_an_item_that_is_not_marked_is_never_sent(self):
		self.new_item("Plain", send_as=None)
		self.assertEqual(self.logs_of(), [])

	def test_switched_off_sends_nothing(self):
		self.conn_a.reload()
		self.conn_a.push_ingredients = 0
		self.conn_a.save()
		self.new_item("Off")
		self.assertEqual(self.logs_of(), [])

	def test_a_name_that_is_used_says_what_to_do(self):
		self.fake_a.add_ingredient("existing", f"{TAG}Bag", "pcs")
		item = self.new_item("Bag")
		(log,) = self.logs_of("erpnext.ingredient_create")
		self.assertEqual(log.status, "Failed")
		self.assertIn("already has an ingredient named", log.message)
		self.assertFalse(frappe.db.get_value("Item", item.name, "koomyo_id"))

	def test_a_uom_with_no_koomyo_unit_names_the_map(self):
		self.new_item("Odd", uom="Box")
		(log,) = self.logs_of("erpnext.ingredient_create")
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn("UOM Map", log.message)

	def test_a_supply_needs_the_supplies_switch(self):
		self.fake_a.supplies_on = False
		self.new_item("Soap", send_as="Supply")
		(log,) = self.logs_of("erpnext.ingredient_create")
		self.assertEqual(log.status, "Failed")
		self.assertIn("Supplies switch is off", log.message)


class TestUpdate(IngredientsOutCase):
	def test_a_new_name_and_a_disabled_item_are_patched(self):
		item = self.linked_item()
		item.item_name = f"{TAG}Bag Renamed"
		item.disabled = 1
		self.save(item)
		(log,) = self.logs_of("erpnext.ingredient_update")
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		((_p, body),) = self.calls("/ingredients/")
		self.assertEqual(body, {"name": f"{TAG}Bag Renamed", "active": False})

	def test_what_koomyo_already_holds_sends_nothing_back(self):
		item = self.linked_item()
		self.fake_a.ingredients[item.koomyo_id]["name"] = f"{TAG}Same"
		item.item_name = f"{TAG}Same"
		self.save(item)
		(log,) = self.logs_of("erpnext.ingredient_update")
		self.assertEqual(log.status, "Skipped")
		self.assertEqual(self.calls("/ingredients/"), [])

	def test_a_unit_cannot_change_once_in_use(self):
		item = self.linked_item()
		self.fake_a.ingredients[item.koomyo_id]["_in_use"] = True
		item.stock_uom = "Kg"
		self.save(item)
		(log,) = [l for l in self.logs_of("erpnext.ingredient_update")]
		self.assertEqual(log.status, "Failed")
		self.assertIn("unit cannot change", log.message)
		self.assertEqual(self.fake_a.ingredients[item.koomyo_id]["unit"], "pcs")

	def test_a_unit_that_is_not_in_use_changes(self):
		item = self.linked_item()
		item.stock_uom = "Kg"
		self.save(item)
		self.assertEqual(self.fake_a.ingredients[item.koomyo_id]["unit"], "kg")

	def test_a_name_that_is_used_says_so(self):
		item = self.linked_item()
		self.fake_a.add_ingredient("other", f"{TAG}Taken", "pcs")
		item.item_name = f"{TAG}Taken"
		self.save(item)
		(log,) = self.logs_of("erpnext.ingredient_update")
		self.assertEqual(log.status, "Failed")
		self.assertIn("already has an ingredient named", log.message)

	def supplier(self, koomyo_id="sup-9", active=True):
		self.fake_a.suppliers[koomyo_id] = {
			"id": koomyo_id,
			"object": "supplier",
			"name": "S",
			"active": active,
		}
		name = f"{TAG}Supplier {koomyo_id}"
		if not frappe.db.exists("Supplier", name):
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": name,
					"supplier_group": "All Supplier Groups",
					"koomyo_connection": self.conn_a.name,
					"koomyo_id": koomyo_id,
					"koomyo_branch": "origin:koomyo",
				}
			).insert(ignore_permissions=True)
		return name

	def test_the_default_supplier_is_set_and_then_removed_when_cleared(self):
		item = self.linked_item()
		supplier = self.supplier()
		item.append("item_defaults", {"company": self.conn_a.company, "default_supplier": supplier})
		self.save(item)
		((_p, body),) = self.calls("/ingredients/")
		self.assertEqual(body, {"supplier_id": "sup-9"})
		self.assertEqual(self.fake_a.ingredients[item.koomyo_id]["default_supplier_id"], "sup-9")
		item = frappe.get_doc("Item", item.name)
		item.set("item_defaults", [])
		self.save(item)
		self.assertIsNone(self.fake_a.ingredients[item.koomyo_id]["default_supplier_id"])

	def test_a_default_set_in_koomyo_is_never_removed_by_an_item_that_never_had_one(self):
		item = self.linked_item()
		self.fake_a.ingredients[item.koomyo_id]["default_supplier_id"] = "sup-admin"
		item.description = "touched"
		self.save(item)
		self.assertEqual(self.logs_of("erpnext.ingredient_update"), [])
		self.assertEqual(self.fake_a.ingredients[item.koomyo_id]["default_supplier_id"], "sup-admin")

	def test_a_supplier_that_is_switched_off_is_refused_with_a_message(self):
		item = self.linked_item()
		supplier = self.supplier("sup-off", active=False)
		item.append("item_defaults", {"company": self.conn_a.company, "default_supplier": supplier})
		self.save(item)
		(log,) = self.logs_of("erpnext.ingredient_update")
		self.assertEqual(log.status, "Failed")
		self.assertIn("switched off", log.message)

	def test_reorder_levels_become_low_stock_levels_and_a_removed_row_switches_the_alert_off(self):
		item = self.linked_item()
		item.append(
			"reorder_levels",
			{
				"warehouse": STORES,
				"warehouse_reorder_level": 5,
				"warehouse_reorder_qty": 10,
				"material_request_type": "Purchase",
			},
		)
		self.save(item)
		((_p, body),) = self.calls("/ingredients/")
		self.assertEqual(
			body,
			{
				"levels": [
					{"place": {"type": "branch", "id": "90000000-0000-4000-8000-000000000001"}, "level": "5"}
				]
			},
		)
		item = frappe.get_doc("Item", item.name)
		item.set("reorder_levels", [])
		self.save(item)
		last = self.calls("/ingredients/")[-1][1]
		self.assertEqual(last["levels"][0]["level"], "0")
		self.assertEqual(self.fake_a.ingredients[item.koomyo_id]["low_stock_levels"], [])


class TestDelete(IngredientsOutCase):
	def delete(self, item):
		frappe.delete_doc("Item", item.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		self.run_queue()

	def test_deleting_an_unused_ingredient_deletes_it_in_koomyo(self):
		item = self.linked_item()
		self.delete(item)
		(log,) = self.logs_of("erpnext.ingredient_delete")
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertNotIn(item.koomyo_id, self.fake_a.ingredients)

	def test_one_that_is_in_use_is_switched_off_instead(self):
		item = self.linked_item()
		self.fake_a.ingredients[item.koomyo_id]["_in_use"] = True
		self.delete(item)
		(log,) = self.logs_of("erpnext.ingredient_delete")
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertIn("switched off there instead", log.message)
		self.assertFalse(self.fake_a.ingredients[item.koomyo_id]["active"])


class TestWarehouses(IngredientsOutCase):
	def new_warehouse(self, name="Depot", send=True):
		doc = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": f"{TAG}{name}",
				"company": self.conn_a.company,
				"koomyo_send": 1 if send else 0,
				"address_line_1": "1 Mall Road",
				"city": "Lahore",
			}
		)
		doc.insert()
		frappe.db.commit()
		self.run_queue()
		return doc

	def test_a_marked_warehouse_is_created_and_put_on_the_branch_map(self):
		doc = self.new_warehouse()
		(log,) = self.logs_of("erpnext.warehouse_create")
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		((_p, body),) = self.calls("/warehouses")
		self.assertEqual(body["name"], f"{TAG}Depot")
		self.assertEqual(body["address"], "1 Mall Road, Lahore")
		self.assertEqual(body["external_ref"], {"system": "erpnext", "external_id": doc.name})
		self.conn_a.reload()
		(row,) = [r for r in self.conn_a.branch_map if r.warehouse == doc.name]
		self.assertEqual((row.location_type, row.koomyo_branch_id), ("Warehouse", "wh-new-1"))

	def test_an_unmarked_warehouse_is_not_sent(self):
		self.new_warehouse(send=False)
		self.assertEqual(self.logs_of(), [])

	def test_a_name_that_is_used_and_a_branch_limited_key_say_why(self):
		self.fake_a.warehouse_records["w"] = {"id": "w", "name": f"{TAG}Depot", "external_refs": {}}
		self.new_warehouse()
		(log,) = self.logs_of("erpnext.warehouse_create")
		self.assertEqual(log.status, "Failed")
		self.assertIn("already has a warehouse named", log.message)

	def test_a_branch_limited_key_is_told_plainly(self):
		self.fake_a.branch_limited_key = True
		self.new_warehouse("Limited")
		(log,) = self.logs_of("erpnext.warehouse_create")
		self.assertEqual(log.status, "Failed")
		self.assertIn("limited to a branch", log.message)

	def test_a_renamed_or_disabled_warehouse_is_patched_and_stock_blocks_switching_off(self):
		doc = self.new_warehouse()
		self.fake_a.stock_calls.clear()
		doc = frappe.get_doc("Warehouse", doc.name)
		doc.warehouse_name = f"{TAG}Depot Two"
		doc.save()
		frappe.db.commit()
		self.run_queue()
		((_p, body),) = self.calls("/warehouses/")
		self.assertEqual(body, {"name": f"{TAG}Depot Two"})
		self.fake_a.warehouse_records["wh-new-1"]["holds_stock"] = True
		doc = frappe.get_doc("Warehouse", doc.name)
		doc.disabled = 1
		doc.save()
		frappe.db.commit()
		self.run_queue()
		failed = [l for l in self.logs_of("erpnext.warehouse_update") if l.status == "Failed"]
		self.assertEqual(len(failed), 1)
		self.assertIn("still holds stock", failed[0].message)
