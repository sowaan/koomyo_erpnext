"""Set Up From Koomyo: plan (no writes), apply (empty settings only), re-run, undo, checklist."""

from typing import ClassVar

import frappe

from koomyo_erpnext import auto_setup
from koomyo_erpnext.tests.helpers import (
	BASE_A,
	KEY_A,
	SECRET_A,
	TENANT_A,
	KoomyoIntegrationTestCase,
	make_connection,
)

COMPANY_D = "_Koomyo Test D"
FRESH = "Fresh Setup"
BRANCH = "90000000-0000-4000-8000-000000000001"


def method(code, label, cash):
	return {
		"object": "payment_method",
		"code": code,
		"label": label,
		"settles_as_cash": cash,
		"active": True,
		"collected": "prepaid",
		"adjustment_percent": "0.00",
		"adjustment_amount": "0.00",
		"adjustment_label": None,
	}


class TestAutoSetup(KoomyoIntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from koomyo_erpnext.tests.helpers import ensure_company

		ensure_company(COMPANY_D, "_KTD")
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		self.before = {d: set(frappe.get_all(d, filters=f, pluck="name")) for d, f in self.SNAPSHOT.items()}
		self.fake_a.lists["/payment-methods"] = [
			method("cash", "Cash", True),
			method("card", "Card", False),
			method("jazz", "Jazz Wallet", False),
		]
		self.fake_a.add_ingredient("i1", "Cheese", "kg")
		self.fake_a.add_ingredient("i2", "Dough", "pcs")
		self.fake_a.add_ingredient("i3", "Oil", "barrel")
		self.fake_a.lists["/ingredients"] = list(self.fake_a.ingredients.values())
		for stale in frappe.get_all("Koomyo Connection", filters={"connection_name": FRESH}, pluck="name"):
			for table in (
				"Koomyo Branch Map",
				"Koomyo Payment Map",
				"Koomyo Tax Map",
				"Koomyo UOM Map",
				"Koomyo Waste Map",
				"Koomyo Setup Log",
				"Koomyo Discount Map",
			):
				frappe.db.delete(table, {"parent": stale})
		frappe.db.delete("Koomyo Connection", {"connection_name": FRESH})
		self.conn = make_connection(
			FRESH, COMPANY_D, BASE_A, KEY_A, SECRET_A, tenant_id=TENANT_A, enabled=False
		)
		self.conn.db_set("status", "Connected")
		self.conn.reload()

	SNAPSHOT: ClassVar[dict] = {
		"Account": {"company": COMPANY_D},
		"Cost Center": {"company": COMPANY_D},
		"Warehouse": {"company": COMPANY_D},
		"Mode of Payment": {},
		"Customer": {},
		"Item Group": {},
		"Price List": {},
		"Supplier Group": {},
	}

	def tearDown(self):
		# Remove only what this test made, never records that were already on the site.
		for doctype, filters in reversed(list(self.SNAPSHOT.items())):
			for name in set(frappe.get_all(doctype, filters=filters, pluck="name")) - self.before[doctype]:
				if doctype == "Mode of Payment":
					frappe.db.delete("Mode of Payment Account", {"parent": name})
				frappe.db.delete(doctype, {"name": name})
		frappe.db.commit()
		super().tearDown()

	def plan(self):
		with self.koomyo():
			koomyo = auto_setup.read_koomyo(self.conn)
		return koomyo, {r["id"]: r for r in auto_setup.build_plan(self.conn, koomyo)}

	def apply(self, koomyo=None, tweak=None):
		koomyo = koomyo or self.plan()[0]
		rows = auto_setup.build_plan(self.conn, koomyo)
		choices = [{"id": r["id"], "action": r["action"], "value": r["value"]} for r in rows]
		for c in choices:
			if tweak:
				tweak(c)
		result = auto_setup.apply_plan(self.conn, koomyo, choices)
		self.conn.reload()
		return result

	def counts(self):
		return {
			d: frappe.db.count(d, {"company": COMPANY_D}) for d in ("Account", "Cost Center", "Warehouse")
		} | {"Mode of Payment": frappe.db.count("Mode of Payment"), "Customer": frappe.db.count("Customer")}

	def test_central_warehouses_found_from_transfers_are_mapped(self):
		central = "61111d66-8b23-4992-a9e7-e1508eb02751"
		self.fake_a.lists["/stock-transfers"] = [
			{
				"id": "t1",
				"from": {"id": central, "name": "Main Warehouse", "type": "warehouse"},
				"to": {"id": BRANCH, "name": "Pizza Flex Bahadurabad", "type": "branch"},
			},
			{
				"id": "t2",
				"from": {"id": BRANCH, "name": "Pizza Flex Bahadurabad", "type": "branch"},
				"to": {"id": central, "name": "Main Warehouse", "type": "warehouse"},
			},
		]
		_koomyo, plan = self.plan()
		row = plan[f"central:{central}"]
		self.assertEqual((row["doctype"], row["section"]), ("Warehouse", "Central warehouses"))
		self.assertEqual(len([k for k in plan if k.startswith("central:")]), 1, "one row per warehouse")
		self.apply()
		mapped = {r.koomyo_branch_id: r for r in self.conn.branch_map}
		self.assertEqual(mapped[central].location_type, "Warehouse")
		self.assertEqual(mapped[central].koomyo_branch_name, "Main Warehouse")
		self.assertEqual(frappe.db.get_value("Warehouse", mapped[central].warehouse, "company"), COMPANY_D)
		self.assertEqual(mapped[BRANCH].location_type, "Branch")

	def test_no_transfers_means_no_central_warehouse_rows(self):
		_koomyo, plan = self.plan()
		self.assertEqual([k for k in plan if k.startswith("central:")], [])

	def test_plan_writes_nothing_and_matches_what_exists(self):
		before = self.counts()
		koomyo, plan = self.plan()
		self.assertEqual(self.counts(), before)
		self.assertEqual(plan[f"branch:{BRANCH}:cost_center"]["action"], auto_setup.CREATE)
		self.assertEqual(
			plan["payment:cash:account"]["value"], "Cash - _KTD", "cash methods use the company cash account"
		)
		self.assertEqual(
			(plan["payment:card:account"]["action"], plan["payment:card:account"]["value"]),
			(auto_setup.CREATE, "Card Clearing"),
		)
		self.assertEqual(plan["uom:kg"]["value"], "Kg")
		self.assertEqual(plan["uom:pcs"]["value"], "Nos")
		self.assertEqual((plan["uom:barrel"]["action"], plan["uom:barrel"]["value"]), (auto_setup.SKIP, ""))
		self.assertEqual(plan["field:income_account"]["value"], "Sales - _KTD")
		self.assertEqual(plan["field:cogs_account"]["value"], "Cost of Goods Sold - _KTD")
		self.assertEqual(
			plan["tax:account"]["value"] in ("GST - _KTD",), True, "an existing Tax account is used"
		)
		self.assertTrue(koomyo["account"]["name"])

	def test_existing_accounts_are_used_not_duplicated(self):
		from koomyo_erpnext.tests.helpers import ensure_account

		ensure_account(COMPANY_D, "_KTD", "Delivery Income", "Direct Income")
		_, plan = self.plan()
		self.assertEqual(
			(plan["field:delivery_fee_account"]["action"], plan["field:delivery_fee_account"]["value"]),
			(auto_setup.USE, "Delivery Income - _KTD"),
		)
		before = frappe.db.count("Account", {"company": COMPANY_D, "account_name": "Delivery Income"})
		self.apply()
		self.assertEqual(
			frappe.db.count("Account", {"company": COMPANY_D, "account_name": "Delivery Income"}), before
		)

	def test_apply_fills_the_connection_and_logs_what_it_made(self):
		result = self.apply()
		c = self.conn
		self.assertEqual([r.koomyo_branch_id for r in c.branch_map], [BRANCH])
		self.assertTrue(
			frappe.db.get_value("Cost Center", c.branch_map[0].cost_center, "company") == COMPANY_D
		)
		self.assertEqual(sorted(r.koomyo_method for r in c.payment_map), ["card", "cash", "jazz"])
		self.assertEqual({r.koomyo_method: r.account for r in c.payment_map}["cash"], "Cash - _KTD")
		self.assertEqual(len(c.tax_map), 1)
		self.assertEqual(
			sorted(r.source for r in c.discount_map),
			["gift_item", "manual", "points_reward", "promotion", "slice_reward", "voucher"],
		)
		self.assertEqual(
			sorted(r.koomyo_unit for r in c.uom_map), ["kg", "pcs"], "the unknown unit is left for the user"
		)
		self.assertEqual(
			sorted(r.reason for r in c.waste_map), ["comp", "other", "spoilage", "staff_meal", "tasting"]
		)
		for field in (
			"income_account",
			"cogs_account",
			"walk_in_customer",
			"price_list",
			"menu_item_group",
			"ingredient_item_group",
			"supplier_group",
			"main_warehouse",
			"transit_warehouse",
			"cash_over_short_account",
			"purchase_tax_account",
			"purchase_withholding_account",
			"purchase_charge_account",
			"purchase_charge_expense_account",
			"customer_group",
			"territory",
		):
			self.assertTrue(c.get(field), field)
		# Everything it picked belongs to this Company (checked again by the connection's own validation).
		self.assertTrue(result["created"])
		self.assertEqual(len(c.setup_log), len(result["created"]))
		for flag in (
			"sync_sales",
			"sync_refunds",
			"sync_shifts",
			"sync_stock",
			"sync_purchasing",
			"sync_customers",
		):
			self.assertEqual(c.get(flag), 0, "no sync is switched on")
		self.assertEqual((c.enabled, c.dry_run), (0, 1), "a fresh connection starts in Dry Run")

	def test_rerun_changes_nothing_and_never_overwrites(self):
		self.conn.income_account = "Service - _KTD"
		self.conn.save()
		self.apply()
		self.assertEqual(self.conn.income_account, "Service - _KTD", "a filled setting is never changed")
		before, accounts = self.counts(), len(self.conn.setup_log)
		_, plan = self.plan()
		self.assertNotIn("field:income_account", plan)
		self.assertEqual(
			[k for k in plan if k.startswith(("branch:", "payment:", "tax:", "waste:", "central:"))], []
		)
		self.apply()
		self.assertEqual(self.counts(), before)
		self.assertEqual(len(self.conn.setup_log), accounts)

	def test_another_companys_record_is_refused(self):
		def tweak(choice):
			if choice["id"] == "field:income_account":
				choice.update(action=auto_setup.USE, value="Sales - _KTA")

		with self.assertRaises(frappe.ValidationError) as ctx:
			self.apply(tweak=tweak)
		self.assertIn("_Koomyo Test A", str(ctx.exception))
		frappe.db.rollback()

	def test_user_can_skip_and_choose(self):
		def tweak(choice):
			if choice["id"] == "payment:jazz:account":
				choice.update(action=auto_setup.SKIP)
			if choice["id"] == "uom:barrel":
				choice.update(action=auto_setup.USE, value="Litre")

		self.apply(tweak=tweak)
		self.assertNotIn("jazz", [r.koomyo_method for r in self.conn.payment_map], "skipped: no half row")
		self.assertIn(("barrel", "Litre"), [(r.koomyo_unit, r.uom) for r in self.conn.uom_map])

	def test_undo_removes_unused_and_keeps_used(self):
		def own_group(choice):
			if choice["id"] == "field:menu_item_group":
				choice.update(action=auto_setup.CREATE, value="_KTEST Menu Group")

		self.apply(tweak=own_group)
		group = self.conn.menu_item_group
		self.assertEqual(group, "_KTEST Menu Group")
		frappe.db.delete("Item", {"name": "_KTEST-USES-GROUP"})
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "_KTEST-USES-GROUP",
				"item_group": group,
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()
		self.conn.reload()
		result = auto_setup.undo_plan(self.conn)
		self.assertTrue(result["removed"])
		self.assertIn(f"Item Group {group}", result["kept"], "something uses it, so it stays")
		self.assertTrue(frappe.db.exists("Item Group", group))
		self.conn.reload()
		self.assertEqual(self.conn.menu_item_group, group, "and the connection still points at it")
		self.assertEqual([r.record_name for r in self.conn.setup_log], [group])
		self.assertEqual(
			[r.koomyo_method for r in self.conn.payment_map],
			["cash"],
			"rows pointing at removed records are dropped; the cash row uses records Set Up did not create",
		)
		frappe.delete_doc("Item", "_KTEST-USES-GROUP", force=True)

	def test_read_only_connection_can_set_up(self):
		self.conn.access_mode = "Read Only"
		self.conn.save()
		self.apply()
		self.assertTrue(self.conn.branch_map)
		self.assertEqual(self.fake_a.writes, [], "Set Up only reads from Koomyo")

	def test_checklist(self):
		todo = auto_setup.checklist(self.conn)
		texts = " ".join(t["text"] for t in todo)
		self.assertIn("Branch Map is empty", texts)
		self.conn.sync_sales = 1
		texts = " ".join(t["text"] for t in auto_setup.checklist(self.conn))
		self.assertIn("Payment Map is empty", texts)
		self.assertIn("Income Account", texts)
		self.apply()
		self.conn.sync_sales = 1
		self.conn.sync_refunds = 1
		self.conn.webhook_secret = "whsec_test"
		self.assertEqual([t["text"] for t in auto_setup.checklist(self.conn)], [])
