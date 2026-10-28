"""Section 7: supplies, low stock, deleted open counts, and payment methods that are not in the Payment Map."""

import copy
from decimal import Decimal as D

import frappe

from koomyo_erpnext.tests.fake_koomyo import load_fixture
from koomyo_erpnext.tests.helpers import TENANT_A, gl
from koomyo_erpnext.tests.test_sales import SalesTestCase, order_event
from koomyo_erpnext.tests.test_stock import KITCHEN, STORES, StockTestCase, adjustment

SOAP = "7a1c2b3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


class TestSupplies(StockTestCase):
	def setUp(self):
		super().setUp()
		self.fake_a.add_ingredient(SOAP, "Hand soap", "pcs", "50.0000")
		self.fake_a.ingredients[SOAP].update({"kind": "supply", "supply_category": "Cleaning"})
		self.conn_a.reload()
		self.conn_a.supplies_expense_account = "Miscellaneous Expenses - _KTA"
		self.conn_a.save()
		self.assertEqual(
			self.book(adjustment(1, "20.0000", ingredient=SOAP, unit_cost="50.0000")).status, "Done"
		)

	def test_the_item_remembers_its_kind(self):
		item = frappe.get_doc("Item", self.item(SOAP))
		self.assertEqual(item.koomyo_kind, "supply")
		self.assertEqual(
			frappe.db.get_value("Item", self.item(KITCHEN) or self.item(SOAP), "is_stock_item"), 1
		)

	def test_a_supply_issue_is_an_expense_not_a_stock_adjustment_or_food_cost(self):
		event = adjustment(2, "-3.0000", ingredient=SOAP, unit_cost="50.0000", movement_id="issue-1")
		event["data"]["reason"] = "supply_issue"
		log = self.book(event)
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		entry = gl("Stock Entry", log.reference_name)
		self.assertEqual(entry["Miscellaneous Expenses - _KTA"], D("150.00"))
		self.assertNotIn("Stock Adjustment - _KTA", entry)
		self.assertNotIn("Cost of Goods Sold - _KTA", entry)
		self.assertEqual(self.qty(STORES, SOAP), D("17"))

	def test_undoing_a_mistaken_issue_puts_it_back_on_the_same_account(self):
		event = adjustment(2, "3.0000", ingredient=SOAP, unit_cost="50.0000", movement_id="issue-undo")
		event["data"]["reason"] = "supply_issue"
		log = self.book(event)
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertEqual(
			frappe.db.get_value("Stock Entry", log.reference_name, "purpose"), "Material Receipt"
		)

	def test_without_the_account_the_issue_names_the_setting(self):
		self.conn_a.reload()
		self.conn_a.supplies_expense_account = None
		self.conn_a.save()
		event = adjustment(2, "-1.0000", ingredient=SOAP, unit_cost="50.0000", movement_id="issue-2")
		event["data"]["reason"] = "supply_issue"
		log = self.book(event)
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn("Supplies Expense Account", log.message)

	def test_another_manual_adjustment_still_goes_to_stock_adjustment(self):
		event = adjustment(2, "-1.0000", ingredient=SOAP, unit_cost="50.0000", movement_id="spill-1")
		event["data"]["reason"] = "Spilled"
		log = self.book(event)
		self.assertEqual(log.status, "Done")
		self.assertIn(self.conn_a.stock_adjustment_account, gl("Stock Entry", log.reference_name))

	def test_the_menu_load_keeps_the_kind(self):
		from koomyo_erpnext.sync import catalog

		self.fake_a.lists["/categories"] = []
		self.fake_a.lists["/menu-items"] = []
		self.fake_a.lists["/ingredients"] = [self.fake_a.ingredients[SOAP]]
		self.fake_a.ingredients[SOAP]["kind"] = "packaging"
		with self.koomyo():
			catalog.load_catalog(self.conn_a.name)
		self.assertEqual(frappe.db.get_value("Item", self.item(SOAP), "koomyo_kind"), "packaging")


class TestLowStock(StockTestCase):
	def test_it_is_shown_and_books_nothing(self):
		event = copy.deepcopy(load_fixture("event_stock_low")) | {"tenant_id": TENANT_A, "seq": 900}
		log = self.book(event)
		self.assertEqual(log.status, "Skipped")
		self.assertIn("Low stock: Kitchen stock at Main branch is 8.500", log.message)
		self.assertIn("10.000", log.message)
		self.assertFalse(frappe.get_all("Stock Entry", filters={"koomyo_connection": self.conn_a.name}))

	def test_it_is_asked_for_with_the_stock_scope_only_when_stock_is_on(self):
		from koomyo_erpnext.sync import events

		self.assertIn("stock.low", events.switched_on_types(self.conn_a))
		self.assertIn("stock.read", events.required_scopes(self.conn_a))
		self.conn_a.sync_stock = 0
		self.assertNotIn("stock.low", events.switched_on_types(self.conn_a))


class TestPaymentMethods(SalesTestCase):
	def setUp(self):
		super().setUp()
		from koomyo_erpnext.auto_setup import add_mode_account

		mode = frappe.get_doc("Mode of Payment", "Cash")
		if not [r for r in mode.accounts if r.company == self.conn_a.company]:
			add_mode_account("Cash", self.conn_a.company, "Cash - _KTA")
			self.added_mode_account = True

	def tearDown(self):
		if getattr(self, "added_mode_account", False):
			frappe.db.delete("Mode of Payment Account", {"parent": "Cash", "company": self.conn_a.company})
			frappe.db.commit()
		super().tearDown()

	def sale(self, method, seq=10, order_id="order-pay"):
		return order_event(
			seq=seq,
			order_id=order_id,
			lines=[{"quantity": 1, "unit_price": "100.00", "line_total": "100.00"}],
			payments=[(method, "100.00")],
			items_subtotal="100.00",
			total="100.00",
			amount_due="100.00",
			amount_paid="100.00",
		)

	def test_an_unknown_method_without_a_default_fails_with_the_row_to_add(self):
		log = self.book(self.sale("online_card"))
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn("online_card", log.message)
		self.assertIn("Payment Map", log.message)

	def test_with_a_default_it_is_booked_and_the_log_says_so(self):
		self.conn_a.reload()
		self.conn_a.default_payment_mode = "Cash"
		self.conn_a.save()
		log = self.book(self.sale("online_card"))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertIn("'online_card' is not in the Payment Map", log.message)
		self.assertIn("Default Mode of Payment Cash", log.message)
		invoice = self.invoice("order-pay")
		(pe,) = self.payments(invoice)
		self.assertEqual(pe.mode_of_payment, "Cash")
		self.assertIn("not in the Payment Map", pe.remarks)
		self.assertEqual(
			gl("Payment Entry", pe.name), {"Cash - _KTA": D("100.00"), "Debtors - _KTA": D("-100.00")}
		)

	def test_a_mapped_method_does_not_warn(self):
		self.conn_a.reload()
		self.conn_a.default_payment_mode = "Cash"
		self.conn_a.save()
		log = self.book(self.sale("cash", order_id="order-cash"))
		self.assertEqual((log.status, log.message or ""), ("Done", ""))

	def test_a_mode_without_an_account_for_the_company_is_still_a_mapping_problem(self):
		if not frappe.db.exists("Mode of Payment", "_Koomyo No Account"):
			frappe.get_doc(
				{"doctype": "Mode of Payment", "mode_of_payment": "_Koomyo No Account", "type": "General"}
			).insert()
		self.conn_a.reload()
		self.conn_a.default_payment_mode = "_Koomyo No Account"
		self.conn_a.save()
		log = self.book(self.sale("online_card"))
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn("default account", log.message)

	def test_a_refund_by_an_unknown_method_follows_the_same_rule(self):
		from koomyo_erpnext.tests.test_sales import refund_event

		self.conn_a.reload()
		self.conn_a.default_payment_mode = "Cash"
		self.conn_a.save()
		self.assertEqual(self.book(self.sale("cash", order_id="order-r")).status, "Done")
		log = self.book(refund_event(order_id="order-r", amount="100.00", method="online_card"))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertIn("is not in the Payment Map", log.message)
