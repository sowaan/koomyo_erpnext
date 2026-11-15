"""A row for a Koomyo Item may only use a unit the Item defines, at the Item's own conversion."""

import frappe

from koomyo_erpnext.tests.test_purchasing import PurchasingTestCase


class TestUnitGuard(PurchasingTestCase):
	def order(self, uom=None, factor=None, item=None):
		"""A draft Purchase Order in ERPNext whose one row uses `uom` (and `factor` when given)."""
		po = frappe.new_doc("Purchase Order")
		po.update(
			{
				"company": "_Koomyo Test A",
				"supplier": "_Koomyo Dairy",
				"transaction_date": "2026-10-12",
				"schedule_date": "2026-10-13",
				"set_warehouse": "Stores - _KTA",
				"currency": "PKR",
				"conversion_rate": 1,
			}
		)
		row = {
			"item_code": item or self.cheese,
			"qty": 20,
			"rate": 7,
			"warehouse": "Stores - _KTA",
			"schedule_date": "2026-10-13",
		}
		if uom:
			row["uom"] = uom
		if factor is not None:
			row["conversion_factor"] = factor
		po.append("items", row)
		return po

	def give_pack(self):
		item = frappe.get_doc("Item", self.cheese)
		item.append("uoms", {"uom": "Box", "conversion_factor": 25})
		item.save()

	def test_a_unit_the_item_does_not_define_is_refused_with_what_is_allowed(self):
		self.give_pack()
		with self.assertRaises(frappe.ValidationError) as caught:
			self.order("Set", 1).insert()
		message = str(caught.exception)
		self.assertIn("Set", message)
		self.assertIn("Box", message)
		self.assertIn("Kg", message)

	def test_the_stock_unit_and_a_defined_pack_are_accepted(self):
		self.give_pack()
		self.assertEqual(self.order().insert().items[0].uom, "Kg")
		po = self.order("Box").insert()
		self.assertEqual((po.items[0].uom, po.items[0].conversion_factor), ("Box", 25))

	def test_a_typed_conversion_that_is_not_the_items_is_refused(self):
		self.give_pack()
		with self.assertRaises(frappe.ValidationError) as caught:
			self.order("Box", 10).insert()
		self.assertIn("25", str(caught.exception))

	def test_an_item_that_does_not_come_from_koomyo_is_left_alone(self):
		code = "_KT-PLAIN-ITEM"
		if not frappe.db.exists("Item", code):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_group": frappe.db.get_value("Item", self.cheese, "item_group"),
					"stock_uom": "Nos",
					"is_stock_item": 1,
				}
			).insert()
		try:
			po = self.order("Set", 1, item=code).insert()
			self.assertEqual(po.items[0].uom, "Set")
		finally:
			frappe.db.rollback()

	def test_the_same_rule_holds_for_a_material_request_and_a_stock_entry(self):
		mr = frappe.new_doc("Material Request")
		mr.update(
			{
				"material_request_type": "Purchase",
				"company": "_Koomyo Test A",
				"transaction_date": "2026-10-12",
				"schedule_date": "2026-10-13",
			}
		)
		mr.append(
			"items",
			{
				"item_code": self.cheese,
				"qty": 5,
				"uom": "Set",
				"conversion_factor": 1,
				"warehouse": "Stores - _KTA",
			},
		)
		with self.assertRaises(frappe.ValidationError):
			mr.insert()
		se = frappe.new_doc("Stock Entry")
		se.update(
			{
				"stock_entry_type": "Material Receipt",
				"company": "_Koomyo Test A",
				"to_warehouse": "Stores - _KTA",
			}
		)
		se.append(
			"items",
			{
				"item_code": self.cheese,
				"qty": 1,
				"uom": "Set",
				"conversion_factor": 1,
				"t_warehouse": "Stores - _KTA",
				"basic_rate": 1,
			},
		)
		with self.assertRaises(frappe.ValidationError):
			se.insert()
