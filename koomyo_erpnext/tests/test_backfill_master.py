"""Backfill of master data only: suppliers, customers, the menu and ingredients, branches and warehouses.

These come in whole, so no date range is asked for. A preview writes nothing; a second run changes nothing.
"""

import copy

import frappe

from koomyo_erpnext.tests.test_backfill import BackfillTestCase
from koomyo_erpnext.tests.test_catalog import CATEGORY, COLA, menu_item
from koomyo_erpnext.tests.test_stock import KITCHEN


class MasterCase(BackfillTestCase):
	def setUp(self):
		super().setUp()
		self.conn_a.reload()
		self.conn_a.sync_customers = 1
		self.conn_a.save()

	def make_master(self, **ticks):
		doc = frappe.get_doc(
			{
				"doctype": "Koomyo Backfill",
				"connection": self.conn_a.name,
				"bring_sales": 0,
				**ticks,
			}
		)
		doc.insert()
		frappe.db.commit()
		return doc


class TestMasterOnly(MasterCase):
	def test_master_data_alone_needs_no_dates_and_is_one_chunk(self):
		doc = self.make_master(bring_customers=1)
		self.assertEqual([c.chunk for c in doc.chunks], ["customers"])
		self.assertTrue(doc.from_date and doc.to_date)

	def test_a_dated_thing_still_needs_its_dates(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{"doctype": "Koomyo Backfill", "connection": self.conn_a.name, "bring_sales": 1}
			).insert()

	def test_nothing_ticked_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self.make_master()
		self.assertIn("Tick at least one", str(caught.exception))

	def test_customers_alone_run_without_dates(self):
		self.fake_a.lists["/customers"] = []
		doc = self.press(self.make_master(bring_customers=1), "start")
		self.assertEqual((doc.status, doc.failed), ("Completed", 0), doc.error)

	def test_each_master_tick_is_one_chunk_named_for_it(self):
		doc = self.make_master(bring_places=1, bring_catalog=1, bring_suppliers=0, bring_customers=1)
		self.assertEqual([c.chunk for c in doc.chunks], ["places", "catalog", "customers"])


class TestMasterCatalog(MasterCase):
	def setUp(self):
		super().setUp()
		from koomyo_erpnext.tests.helpers import configure_stock

		configure_stock(self.conn_a, "_KTA")
		self.fake_a.add_ingredient(KITCHEN, "Kitchen stock", "pcs", "12.5000")
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
		self.fake_a.lists["/menu-items"] = [menu_item(COLA, "Cola", price="150.00", category=None)]
		self.fake_a.lists["/ingredients"] = [self.fake_a.ingredients[KITCHEN]]

	def tearDown(self):
		frappe.db.delete("Item Group", {"name": f"{self.conn_a.name} Menu"})
		for group in frappe.get_all(
			"Item Group", filters={"name": ("like", f"%({self.conn_a.name})")}, pluck="name"
		):
			frappe.db.delete("Item Group", {"name": group})
		frappe.db.commit()
		super().tearDown()

	def items(self):
		return frappe.get_all("Item", filters={"koomyo_connection": self.conn_a.name}, pluck="koomyo_id")

	def test_preview_writes_nothing_then_the_run_loads_and_a_second_run_changes_nothing(self):
		before = len(self.items())
		doc = self.press(self.make_master(bring_catalog=1), "preview")
		self.assertEqual((doc.status, doc.failed), ("Previewed", 0), doc.error)
		self.assertGreaterEqual(doc.created, 2)
		self.assertEqual(len(self.items()), before, "a preview writes nothing")
		doc = self.press(self.make_master(bring_catalog=1), "start")
		self.assertEqual((doc.status, doc.failed), ("Completed", 0), doc.error)
		self.assertTrue({COLA, KITCHEN} <= set(self.items()))
		again = self.press(self.make_master(bring_catalog=1), "start")
		self.assertEqual((again.created, again.failed), (0, 0))
		self.assertGreaterEqual(again.already_there, 2)


class TestMasterPlaces(MasterCase):
	NEW_BRANCH = "90000000-0000-4000-8000-0000000000bb"
	NEW_WAREHOUSE = "90000000-0000-4000-8000-0000000000cc"

	def setUp(self):
		super().setUp()
		new = copy.deepcopy(self.fake_a.branches[0])
		new.update({"id": self.NEW_BRANCH, "name": "_BF Branch New", "code": "BFN"})
		self.fake_a.branches = [*self.fake_a.branches, new]
		self.fake_a.lists["/warehouses"] = [
			{
				"id": self.NEW_WAREHOUSE,
				"object": "warehouse",
				"name": "_BF Central",
				"is_main": False,
				"active": True,
			}
		]
		self.conn_a.reload()
		self.mapped = {r.koomyo_branch_id for r in self.conn_a.branch_map}

	def tearDown(self):
		self.conn_a.reload()
		self.conn_a.set(
			"branch_map", [r for r in self.conn_a.branch_map if r.koomyo_branch_id in self.mapped]
		)
		self.conn_a.set("setup_log", [])
		self.conn_a.save()
		for doctype, filters in (
			("Warehouse", {"warehouse_name": ("like", "_BF %")}),
			("Cost Center", {"cost_center_name": ("like", "_BF %")}),
		):
			for name in frappe.get_all(doctype, filters=filters, pluck="name"):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def rows(self):
		self.conn_a.reload()
		return {r.koomyo_branch_id: r for r in self.conn_a.branch_map}

	def test_preview_counts_what_is_missing_and_writes_nothing(self):
		doc = self.press(self.make_master(bring_places=1), "preview")
		self.assertEqual((doc.status, doc.failed), ("Previewed", 0), doc.error)
		self.assertEqual(doc.created, 2, "the new branch and the new warehouse")
		self.assertNotIn(self.NEW_BRANCH, self.rows())
		self.assertFalse(frappe.db.exists("Warehouse", {"warehouse_name": "_BF Central"}))

	def test_the_run_fills_the_branch_map_and_a_second_run_changes_nothing(self):
		doc = self.press(self.make_master(bring_places=1), "start")
		self.assertEqual((doc.status, doc.created, doc.failed), ("Completed", 2, 0), doc.error)
		rows = self.rows()
		self.assertEqual(rows[self.NEW_BRANCH].location_type, "Branch")
		self.assertTrue(rows[self.NEW_BRANCH].warehouse and rows[self.NEW_BRANCH].cost_center)
		self.assertEqual(rows[self.NEW_WAREHOUSE].location_type, "Warehouse")
		count = len(rows)
		again = self.press(self.make_master(bring_places=1), "start")
		self.assertEqual((again.created, again.failed), (0, 0))
		self.assertEqual(len(self.rows()), count)

	def test_places_already_mapped_are_left_alone(self):
		doc = self.press(self.make_master(bring_places=1), "start")
		self.assertEqual(doc.created, 2)
		self.assertTrue(self.mapped <= set(self.rows()))
