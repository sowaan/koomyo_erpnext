"""An Item's unit and pack size follow its Koomyo ingredient (sync/item_units.py).

Straw was Pack in ERPNext; Koomyo now stocks it in pcs and buys it in packs of 100. Items with no transactions are
aligned by themselves, those with transactions are flagged once and left alone, and a repeat changes nothing.
"""

import copy

import frappe

from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry
from koomyo_erpnext.sync import catalog, events, item_units
from koomyo_erpnext.sync.items import get_or_create_item
from koomyo_erpnext.tests.test_stock import STORES, StockTestCase

STRAW = "5c8a2d73-1c2f-4a1f-8e44-3a7a6b5c9f77"
SALT = "5c8a2d73-1c2f-4a1f-8e44-3a7a6b5c9f78"
ODD = "5c8a2d73-1c2f-4a1f-8e44-3a7a6b5c9f79"


class UnitCase(StockTestCase):
	def setUp(self):
		super().setUp()
		self.conn_a.reload()
		self.conn_a.append("uom_map", {"koomyo_unit": "pack", "uom": "Box"})
		self.conn_a.save()
		self.fake_a.lists["/categories"] = []
		self.fake_a.lists["/menu-items"] = []

	def ingredient(self, ingredient_id, name, unit="pcs", pack=("pack", "100")):
		record = self.fake_a.add_ingredient(ingredient_id, name, unit, "1.0000")
		record["purchase_unit"], record["purchase_unit_factor"] = pack if pack else (None, None)
		return record

	def item_for(self, ingredient_id, name, uom="Box"):
		"""An Item as ERPNext made it before the unit changed in Koomyo (its stock UOM is `uom`)."""
		code = get_or_create_item(
			self.conn_a,
			ingredient_id,
			name,
			is_stock_item=1,
			item_group="_Koomyo Ingredients",
			uom=uom,
			is_purchase_item=1,
		)
		return frappe.get_doc("Item", code)

	def reload(self, code):
		return frappe.get_doc("Item", code)

	def rows(self, item):
		return {r.uom: r.conversion_factor for r in item.uoms}

	def heal(self, dry_run=False):
		self.fake_a.lists["/ingredients"] = list(self.fake_a.ingredients.values())
		with self.koomyo():
			return item_units.heal_connection(self.conn_a.name, dry_run=dry_run)

	def attention(self):
		return frappe.get_all(
			"Koomyo Event Log",
			filters={"connection": self.conn_a.name, "event_type": item_units.EVENT_TYPE},
			fields=["name", "status", "error_class", "message", "event_id"],
		)

	def put_stock_on(self, item):
		frappe.get_doc({"doctype": "Bin", "item_code": item.name, "warehouse": STORES}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()


class TestAlign(UnitCase):
	def test_an_item_without_transactions_is_aligned_with_its_pack_row_and_purchase_uom(self):
		self.ingredient(STRAW, "Straw")
		item = self.item_for(STRAW, "Straw")
		summary = self.heal()
		self.assertEqual((summary["aligned"], summary["blocked"], summary["errors"]), (1, 0, []))
		item = self.reload(item.name)
		self.assertEqual((item.stock_uom, item.purchase_uom), ("Nos", "Box"))
		self.assertEqual(self.rows(item), {"Nos": 1, "Box": 100})
		notes = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Item", "reference_name": item.name},
			pluck="content",
		)
		self.assertTrue([n for n in notes if "Before: stock UOM Box" in n and "After: stock UOM Nos" in n])

	def test_a_repeat_changes_nothing(self):
		self.ingredient(STRAW, "Straw")
		item = self.item_for(STRAW, "Straw")
		self.heal()
		modified = self.reload(item.name).modified
		again = self.heal()
		self.assertEqual((again["aligned"], again["pack"], again["blocked"], again["items"]), (0, 0, 0, []))
		self.assertEqual(self.reload(item.name).modified, modified)

	def test_a_dry_run_lists_and_writes_nothing(self):
		self.ingredient(STRAW, "Straw")
		item = self.item_for(STRAW, "Straw")
		summary = self.heal(dry_run=True)
		self.assertEqual(summary["aligned"], 1)
		self.assertIn("stock UOM Box to Nos", summary["items"][0]["changes"][0])
		self.assertEqual(self.reload(item.name).stock_uom, "Box")
		self.assertEqual(self.attention(), [])

	def test_no_pack_in_koomyo_leaves_the_item_with_its_stock_unit_only(self):
		self.ingredient(STRAW, "Straw", pack=None)
		item = self.item_for(STRAW, "Straw")
		self.heal()
		item = self.reload(item.name)
		self.assertEqual((item.stock_uom, self.rows(item)), ("Nos", {"Nos": 1}))
		self.assertFalse(item.purchase_uom)

	def test_an_item_that_already_matches_gets_only_the_missing_pack_row(self):
		self.ingredient(STRAW, "Straw")
		item = self.item_for(STRAW, "Straw", uom="Nos")
		self.heal()
		item = self.reload(item.name)
		self.assertEqual((item.stock_uom, self.rows(item)), ("Nos", {"Nos": 1, "Box": 100}))


class TestTransactions(UnitCase):
	def test_an_item_with_transactions_is_untouched_and_flagged_once(self):
		self.ingredient(STRAW, "Straw")
		item = self.item_for(STRAW, "Straw")
		self.put_stock_on(item)
		first = self.heal()
		self.assertEqual((first["blocked"], first["aligned"]), (1, 0))
		self.heal()
		self.heal()
		self.assertEqual(self.reload(item.name).stock_uom, "Box", "left as it was")
		(entry,) = self.attention()
		self.assertEqual((entry.status, entry.error_class), ("Failed", "Fatal"))
		for text in (item.name, "Straw", "Box", "pcs", "Bin (1)"):
			self.assertIn(text, entry.message)

	def test_a_dry_run_names_what_blocks_it_and_writes_no_entry(self):
		self.ingredient(STRAW, "Straw")
		self.put_stock_on(self.item_for(STRAW, "Straw"))
		summary = self.heal(dry_run=True)
		self.assertEqual(summary["blocked"], 1)
		self.assertIn("Bin (1)", summary["items"][0]["blockers"][0])
		self.assertEqual(self.attention(), [])

	def test_after_the_blocker_is_gone_retry_aligns_it(self):
		self.ingredient(STRAW, "Straw")
		item = self.item_for(STRAW, "Straw")
		self.put_stock_on(item)
		self.heal()
		frappe.db.delete("Bin", {"item_code": item.name})
		frappe.db.commit()
		(entry,) = self.attention()
		retry([entry.name])
		with self.koomyo():
			events.process_pending(self.conn_a.name)
		self.assertEqual(frappe.db.get_value("Koomyo Event Log", entry.name, "status"), "Done")
		self.assertEqual(self.reload(item.name).stock_uom, "Nos")

	def test_a_pack_size_that_changed_is_kept_with_transactions_and_updated_without(self):
		self.ingredient(STRAW, "Straw")
		self.ingredient(SALT, "Salt")
		straw = self.item_for(STRAW, "Straw", uom="Nos")
		salt = self.item_for(SALT, "Salt", uom="Nos")
		for item in (straw, salt):
			item.append("uoms", {"uom": "Box", "conversion_factor": 80})
			item.save()
		self.put_stock_on(straw)
		self.heal()
		self.assertEqual(self.rows(self.reload(straw.name))["Box"], 80, "kept: it has transactions")
		self.assertEqual(self.rows(self.reload(salt.name))["Box"], 100, "updated: it has none")
		notes = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Item", "reference_name": straw.name},
			pluck="content",
		)
		self.assertTrue([n for n in notes if "now says Box holds 100" in n])


class TestUnmappedAndOthers(UnitCase):
	def test_an_unmapped_unit_is_a_needs_mapping_entry_once_and_the_others_still_go(self):
		self.ingredient(ODD, "Odd", unit="furlong", pack=None)
		self.ingredient(STRAW, "Straw")
		odd = self.item_for(ODD, "Odd", uom="Nos")
		straw = self.item_for(STRAW, "Straw")
		first = self.heal()
		self.heal()
		self.assertEqual((first["unmapped"], first["aligned"]), (1, 1))
		self.assertEqual(self.reload(straw.name).stock_uom, "Nos")
		self.assertEqual(self.reload(odd.name).stock_uom, "Nos", "untouched")
		(entry,) = self.attention()
		self.assertEqual(entry.error_class, "NeedsMapping")
		self.assertIn("furlong", entry.message)

	def test_items_of_another_connection_or_without_a_koomyo_id_are_never_touched(self):
		self.ingredient(STRAW, "Straw")
		item = self.item_for(STRAW, "Straw")
		frappe.db.set_value("Item", item.name, "koomyo_connection", self.conn_b.name)
		plain = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "_KT-NO-KOOMYO-ID",
				"item_group": "_Koomyo Ingredients",
				"stock_uom": "Box",
				"is_stock_item": 1,
			}
		).insert(ignore_permissions=True)
		try:
			summary = self.heal()
			self.assertEqual((summary["aligned"], summary["items"]), (0, []))
			self.assertEqual(self.reload(item.name).stock_uom, "Box")
			self.assertEqual(self.reload(plain.name).stock_uom, "Box")
		finally:
			frappe.delete_doc("Item", plain.name, force=True, ignore_permissions=True)

	def test_several_items_at_once(self):
		self.ingredient(STRAW, "Straw")
		self.ingredient(SALT, "Salt", unit="kg", pack=("pack", "0.8"))
		a, b = self.item_for(STRAW, "Straw"), self.item_for(SALT, "Salt")
		summary = self.heal()
		self.assertEqual((summary["aligned"], summary["errors"]), (2, []))
		self.assertEqual(self.rows(self.reload(a.name)), {"Nos": 1, "Box": 100})
		self.assertEqual(self.rows(self.reload(b.name)), {"Kg": 1, "Box": 0.8})


class TestEntryPoints(UnitCase):
	def test_the_ingredient_updated_event_aligns_the_item(self):
		self.ingredient(STRAW, "Straw")
		item = self.item_for(STRAW, "Straw")
		event = {
			"id": "evt-ing-1",
			"object": "event",
			"type": "ingredient.updated",
			"seq": 798,
			"tenant_id": self.conn_a.tenant_id,
			"created": "2026-11-05T09:00:00Z",
			"entity": {"type": "ingredient", "id": STRAW},
			"data": copy.deepcopy(self.fake_a.ingredients[STRAW]),
		}
		log = self.book(event)
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertEqual(self.reload(item.name).stock_uom, "Nos")
		again = copy.deepcopy(event)
		again.update({"id": "evt-ing-2", "seq": 799})
		self.assertEqual(self.book(again).status, "Done", "a repeat changes nothing")

	def test_an_event_for_an_ingredient_with_no_item_is_skipped_not_made(self):
		self.ingredient(STRAW, "Straw")
		event = {
			"id": "evt-ing-3",
			"object": "event",
			"type": "ingredient.created",
			"seq": 800,
			"tenant_id": self.conn_a.tenant_id,
			"created": "2026-11-05T09:00:00Z",
			"entity": {"type": "ingredient", "id": STRAW},
			"data": copy.deepcopy(self.fake_a.ingredients[STRAW]),
		}
		log = self.book(event)
		self.assertEqual(log.status, "Skipped")
		self.assertFalse(frappe.db.exists("Item", {"koomyo_id": STRAW}))

	def test_an_event_for_an_item_with_transactions_fails_once_and_then_is_skipped(self):
		self.ingredient(STRAW, "Straw")
		self.put_stock_on(self.item_for(STRAW, "Straw"))
		event = {
			"id": "evt-ing-4",
			"object": "event",
			"type": "ingredient.updated",
			"seq": 801,
			"tenant_id": self.conn_a.tenant_id,
			"created": "2026-11-05T09:00:00Z",
			"entity": {"type": "ingredient", "id": STRAW},
			"data": copy.deepcopy(self.fake_a.ingredients[STRAW]),
		}
		first = self.book(event)
		self.assertEqual((first.status, first.error_class), ("Failed", "Fatal"))
		second = self.book({**copy.deepcopy(event), "id": "evt-ing-5", "seq": 802})
		self.assertEqual(second.status, "Skipped")
		failed = frappe.get_all(
			"Koomyo Event Log",
			filters={
				"connection": self.conn_a.name,
				"status": "Failed",
				"message": ["like", "%has transactions%"],
			},
		)
		self.assertEqual(len(failed), 1, "the first event's own row is the one attention entry")

	def test_the_catalog_backfill_aligns_and_counts_a_blocked_item_as_attention_not_failed(self):
		self.ingredient(STRAW, "Straw")
		self.ingredient(SALT, "Salt", unit="kg", pack=None)
		a = self.item_for(STRAW, "Straw")
		b = self.item_for(SALT, "Salt")
		self.put_stock_on(b)
		self.fake_a.lists["/ingredients"] = [self.fake_a.ingredients[STRAW], self.fake_a.ingredients[SALT]]
		with self.koomyo():
			summary = catalog.load_catalog(self.conn_a.name)
		self.assertEqual(summary["problems"], [])
		self.assertEqual(len(summary["attention"]), 1)
		self.assertEqual(self.reload(a.name).stock_uom, "Nos")
		self.assertEqual(self.reload(b.name).stock_uom, "Box", "has transactions: untouched")
		self.assertEqual(len(self.attention()), 1)
		with self.koomyo():
			again = catalog.load_catalog(self.conn_a.name)
		self.assertEqual(again["ingredients"], {"created": 0, "updated": 0, "unchanged": 2})
		self.assertEqual(len(self.attention()), 1, "raised once")

	def test_the_patch_queues_the_healing_of_each_connection_with_stock_on(self):
		from koomyo_erpnext.patches.align_item_units import execute

		execute()
		queued = [kw.get("connection_name") for _a, kw in self.enqueued if "connection_name" in kw]
		self.assertIn(self.conn_a.name, queued)
