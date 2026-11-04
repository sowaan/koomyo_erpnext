"""Koomyo says how an ingredient is bought (`purchase_unit`, `purchase_unit_factor`): read-only hints.

When the unit maps to a UOM the Item gets that UOM with the factor; nothing is ever stopped for it; Koomyo keeps
quantities and prices in the ingredient's own unit, so an order in packs still arrives as pieces.
"""

import frappe

from koomyo_erpnext.sync import catalog
from koomyo_erpnext.tests.test_purchasing import PurchasingTestCase
from koomyo_erpnext.tests.test_stock import KITCHEN, StockTestCase

STRAW = "5c8a2d73-1c2f-4a1f-8e44-3a7a6b5c9f77"


class PurchaseUnitCase(StockTestCase):
	def setUp(self):
		super().setUp()
		self.conn_a.reload()
		self.conn_a.append("uom_map", {"koomyo_unit": "pack", "uom": "Box"})
		self.conn_a.save()
		self.fake_a.add_ingredient(STRAW, "Straw", "pcs", "0.5000")
		self.fake_a.lists["/categories"] = []
		self.fake_a.lists["/menu-items"] = []

	def load(self, *ingredients):
		self.fake_a.lists["/ingredients"] = list(ingredients)
		with self.koomyo():
			return catalog.load_catalog(self.conn_a.name)

	def straw(self, unit="pack", factor="1000"):
		record = self.fake_a.ingredients[STRAW]
		record.update({"purchase_unit": unit, "purchase_unit_factor": factor})
		return record

	def item(self, ingredient_id=STRAW):
		name = frappe.db.get_value(
			"Item", {"koomyo_connection": self.conn_a.name, "koomyo_id": ingredient_id}
		)
		return frappe.get_doc("Item", name) if name else None


class TestPurchaseUnit(PurchaseUnitCase):
	def test_a_mapped_purchase_unit_becomes_a_uom_of_the_item_and_its_purchase_uom(self):
		summary = self.load(self.straw())
		self.assertEqual(summary["problems"], [])
		item = self.item()
		self.assertEqual((item.stock_uom, item.purchase_uom), ("Nos", "Box"))
		self.assertEqual({r.uom: r.conversion_factor for r in item.uoms}["Box"], 1000)

	def test_null_means_bought_in_its_own_unit_and_changes_nothing(self):
		self.fake_a.ingredients[STRAW].update({"purchase_unit": None, "purchase_unit_factor": None})
		self.load(self.fake_a.ingredients[STRAW], self.fake_a.ingredients[KITCHEN])
		item = self.item()
		self.assertEqual([r.uom for r in item.uoms], ["Nos"])
		self.assertFalse(item.purchase_uom)

	def test_a_purchase_unit_that_is_not_on_the_uom_map_is_skipped_and_the_load_goes_on(self):
		summary = self.load(self.straw(unit="carton"), self.fake_a.ingredients[KITCHEN])
		self.assertEqual(summary["problems"], [])
		self.assertEqual([r.uom for r in self.item().uoms], ["Nos"])
		self.assertTrue(self.item(KITCHEN), "the other ingredient was loaded")

	def test_a_bad_factor_is_skipped_too(self):
		self.load(self.straw(factor="0"))
		self.assertEqual([r.uom for r in self.item().uoms], ["Nos"])
		self.load(self.straw(factor="abc"))
		self.assertEqual([r.uom for r in self.item().uoms], ["Nos"])

	def test_a_second_load_changes_nothing(self):
		self.load(self.straw())
		again = self.load(self.straw())
		self.assertEqual(again["ingredients"], {"created": 0, "updated": 0, "unchanged": 1})

	def test_an_item_that_exists_gets_the_unit_when_koomyo_sets_it_later(self):
		self.load(self.fake_a.ingredients[STRAW])
		self.assertEqual([r.uom for r in self.item().uoms], ["Nos"])
		summary = self.load(self.straw())
		self.assertEqual(summary["ingredients"]["updated"], 1)
		self.assertEqual({r.uom: r.conversion_factor for r in self.item().uoms}["Box"], 1000)

	def test_a_pack_size_that_changed_is_noted_not_applied(self):
		self.load(self.straw(factor="1000"))
		self.load(self.straw(factor="500"))
		item = self.item()
		self.assertEqual({r.uom: r.conversion_factor for r in item.uoms}["Box"], 1000, "kept as it was")
		notes = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Item", "reference_name": item.name},
			pluck="content",
		)
		self.assertTrue([n for n in notes if "now says Box holds 500" in n])

	def test_an_item_made_on_first_sight_by_a_stock_event_gets_it_too(self):
		from koomyo_erpnext.tests.test_stock import adjustment

		self.straw()
		self.assertEqual(self.book(adjustment(1, "10.0000", ingredient=STRAW)).status, "Done")
		self.assertEqual({r.uom: r.conversion_factor for r in self.item().uoms}["Box"], 1000)


class TestOrderInThePurchaseUom(PurchasingTestCase):
	def test_a_purchase_order_in_packs_arrives_as_pieces_at_the_price_per_piece(self):
		self.conn_a.reload()
		self.conn_a.append("uom_map", {"koomyo_unit": "pack", "uom": "Box"})
		self.conn_a.save()
		item = frappe.get_doc("Item", self.cheese)
		item.append("uoms", {"uom": "Box", "conversion_factor": 1000})
		item.save()
		po = self.make_po(qty=5, rate=1450000, submit=False)
		row = po.items[0]
		row.uom, row.conversion_factor = "Box", 1000
		po.save()
		po.submit()
		frappe.db.commit()
		self.run_queue()
		(call,) = self.created("/purchase-orders")
		(line,) = call["body"]["lines"]
		self.assertEqual((line["quantity_ordered"], line["unit_cost"]), ("5000", "1450"))
