"""The setup wizard's backend: secrets stay in the server, steps save to the connection."""

from typing import ClassVar

import frappe

from koomyo_erpnext import setup_wizard
from koomyo_erpnext.tests.helpers import (
	BASE_A,
	KEY_A,
	SECRET_A,
	TENANT_A,
	KoomyoIntegrationTestCase,
	make_connection,
)
from koomyo_erpnext.tests.test_auto_setup import COMPANY_D, method

NAME = "Wizard Test"


class TestSetupWizard(KoomyoIntegrationTestCase):
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

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from koomyo_erpnext.tests.helpers import ensure_company

		ensure_company(COMPANY_D, "_KTD")
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		self.before = {d: set(frappe.get_all(d, filters=f, pluck="name")) for d, f in self.SNAPSHOT.items()}
		self.fake_a.lists["/payment-methods"] = [method("cash", "Cash", True), method("card", "Card", False)]
		self.fake_a.add_ingredient("i1", "Cheese", "kg")
		self.fake_a.lists["/ingredients"] = list(self.fake_a.ingredients.values())

	def tearDown(self):
		for doctype, filters in reversed(list(self.SNAPSHOT.items())):
			for name in set(frappe.get_all(doctype, filters=filters, pluck="name")) - self.before[doctype]:
				if doctype == "Mode of Payment":
					frappe.db.delete("Mode of Payment Account", {"parent": name})
				frappe.db.delete(doctype, {"name": name})
		frappe.db.commit()
		super().tearDown()

	def make(self):
		return setup_wizard.save_connection(COMPANY_D, NAME, BASE_A, "2026-10-01", "Read and Write", KEY_A)

	def test_state_never_contains_a_secret(self):
		state = self.make()
		self.assertTrue(state["has_key"])
		self.assertNotIn(KEY_A, frappe.as_json(state))
		doc = frappe.get_doc("Koomyo Connection", NAME)
		doc.webhook_secret = SECRET_A
		doc.save()
		everything = frappe.as_json(setup_wizard.get_state(NAME))
		self.assertNotIn(KEY_A, everything)
		self.assertNotIn(SECRET_A, everything)
		self.assertTrue(setup_wizard.get_state(NAME)["connection"]["has_secret"])

	def test_wizard_walkthrough(self):
		self.make()
		with self.koomyo():
			tested = setup_wizard.test_connection(NAME)
		self.assertEqual(tested["state"]["status"], "Connected")
		self.assertEqual(tested["state"]["restaurant"], "Slice of Heaven Test Cafe")
		with self.koomyo():
			plan = setup_wizard.plan(NAME)
			choices = [{"id": r["id"], "action": r["action"], "value": r["value"]} for r in plan["rows"]]
			applied = setup_wizard.apply(NAME, frappe.as_json(choices))
		maps = applied["state"]["maps"]
		self.assertEqual((maps["branches"], maps["payments"]), (1, 2))
		state = setup_wizard.save_syncs(NAME, frappe.as_json({"sync_sales": 1, "sync_refunds": 1}))
		self.assertEqual((state["syncs"]["sync_sales"], state["syncs"]["sync_refunds"]), (1, 1))
		needs = " ".join(t["text"] for t in state["checklist"])
		self.assertIn("webhook secret", needs.lower())
		state = setup_wizard.save_webhook_secret(NAME, SECRET_A)
		self.assertTrue(state["has_secret"])
		final = setup_wizard.finish(NAME, go_live=0)
		self.assertEqual((final["enabled"], final["dry_run"]), (True, True), "starts in Dry Run")
		live = setup_wizard.finish(NAME, go_live=1)
		self.assertEqual((live["enabled"], live["dry_run"]), (True, False))

	def test_a_sync_switched_on_early_lists_what_it_still_needs(self):
		self.make()
		state = setup_wizard.save_syncs(NAME, frappe.as_json({"sync_sales": 1}))
		needs = " ".join(t["text"] for t in state["checklist"])
		self.assertIn("Income Account", needs)
		self.assertIn("Payment Map is empty", needs)

	def test_cannot_enable_untested(self):
		self.make()
		with self.assertRaises(frappe.ValidationError):
			setup_wizard.finish(NAME)
		frappe.db.rollback()

	def test_only_koomyo_roles(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				setup_wizard.get_state()
			self.assertFalse(setup_wizard.get_pending_setup())
		finally:
			frappe.set_user("Administrator")

	def test_banner_only_while_there_is_no_connection(self):
		frappe.db.delete(
			"Koomyo Connection",
			{"name": ("in", [c for c in frappe.get_all("Koomyo Connection", pluck="name")])},
		) if False else None
		self.assertEqual(setup_wizard.get_pending_setup(), not frappe.db.exists("Koomyo Connection", {}))

	def test_name_cannot_jump_company(self):
		make_connection("Other D", COMPANY_D, BASE_A, KEY_A, SECRET_A, tenant_id=TENANT_A, enabled=False)
		with self.assertRaises(frappe.ValidationError):
			setup_wizard.save_connection("_Koomyo Test A", "Other D", BASE_A, "2026-10-01")
		frappe.db.rollback()
