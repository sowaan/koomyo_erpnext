"""Per-order stock deduction: each paid order takes out exactly what Koomyo deducted for it."""

from decimal import Decimal as D

import frappe

from koomyo_erpnext.sync import events
from koomyo_erpnext.sync.order_stock import DAILY, PER_ORDER, effective_mode, next_business_day
from koomyo_erpnext.tests.fake_koomyo import load_fixture
from koomyo_erpnext.tests.helpers import TENANT_A, gl
from koomyo_erpnext.tests.test_sales import order_event
from koomyo_erpnext.tests.test_stock import KITCHEN, MOZZARELLA, STORES, StockTestCase, adjustment

BRANCH = "90000000-0000-4000-8000-000000000001"
ORDER = "order-1"


def movement(
	mid, ingredient, qty, reason="order_deduction", order_id=ORDER, at="2026-10-12T16:53:20Z", cost="12.5000"
):
	return {
		"id": mid,
		"object": "stock_movement",
		"ingredient_id": ingredient,
		"ingredient": "x",
		"unit": "pcs",
		"quantity_change": qty,
		"unit_cost": cost,
		"value": "0",
		"currency": "PKR",
		"reason": reason,
		"reference_id": order_id,
		"location": {"type": "branch", "id": BRANCH},
		"created_by": None,
		"created_at": at,
		"business_date": "2026-10-12",
	}


def paid(seq=10, order_id=ORDER):
	return order_event(
		seq=seq,
		order_id=order_id,
		lines=[
			{
				"item_id": "menu-1",
				"name": "Pizza",
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


class TestOrderStock(StockTestCase):
	def setUp(self):
		super().setUp()
		self.per_order()
		self.assertEqual(self.book(adjustment(1, "20.0000")).status, "Done")
		self.assertEqual(
			self.book(adjustment(2, "10.0000", ingredient=MOZZARELLA, unit_cost="1450.0000")).status, "Done"
		)
		self.fake_a.lists["/stock-movements"] = [
			movement("m1", KITCHEN, "-2.0000"),
			movement("m2", MOZZARELLA, "-0.2500", cost="1450.0000"),
			movement("m3", KITCHEN, "-1.0000"),  # a second kitchen round of the same order
			movement("m4", KITCHEN, "-5.0000", order_id="another-order"),  # same minute, other order
		]

	def per_order(self, since="2026-10-01"):
		self.conn_a.reload()
		self.conn_a.db_set(
			{"stock_deduction": PER_ORDER, "stock_deduction_since": since, "stock_deduction_before": None}
		)
		frappe.db.commit()
		self.conn_a.reload()

	def stock_log(self, order_id=ORDER, step="order_stock"):
		return frappe.get_all(
			"Koomyo Event Log",
			filters={"connection": self.conn_a.name, "event_type": f"koomyo.{step}", "entity_id": order_id},
			fields=["*"],
		)

	def test_each_order_takes_out_what_koomyo_deducted(self):
		self.assertEqual(self.book(paid()).status, "Done")
		(log,) = self.stock_log()
		self.assertEqual(log.status, "Done", log.message)
		entry = frappe.get_doc("Stock Entry", log.reference_name)
		self.assertEqual(
			(entry.purpose, entry.koomyo_id, str(entry.posting_date)),
			("Material Issue", f"order:{ORDER}", "2026-10-12"),
		)
		self.assertEqual(
			sorted((row.item_code == self.item(KITCHEN), row.qty) for row in entry.items),
			[(False, 0.25), (True, 3.0)],
		)
		self.assertEqual(self.qty(STORES), D("17"))
		self.assertEqual(self.qty(STORES, MOZZARELLA), D("9.75"))
		self.assertEqual(
			gl("Stock Entry", entry.name)["Cost of Goods Sold - _KTA"], D("400.00")
		)  # 3 x 12.50 + 0.25 x 1450

	def test_daily_usage_is_skipped_on_per_order_days(self):
		self.book(paid())
		usage = load_fixture("event_stock_usage_daily") | {"tenant_id": TENANT_A, "seq": 30}
		usage["data"]["business_date"] = usage["business_date"] = "2026-10-12"
		log = self.book(usage)
		self.assertEqual(log.status, "Skipped")
		self.assertIn("Per Order", log.message)
		self.assertEqual(self.qty(STORES), D("17"), "not counted twice")

	def test_replay_and_backfill_do_not_double(self):
		event = paid()
		self.book(event)
		self.book(dict(event, id="resent", seq=40))
		self.assertEqual(
			frappe.db.count(
				"Stock Entry", {"koomyo_connection": self.conn_a.name, "koomyo_id": f"order:{ORDER}"}
			),
			1,
		)
		self.assertEqual(self.qty(STORES), D("17"))

	def test_cancel_puts_stock_back(self):
		self.book(paid())
		self.fake_a.lists["/stock-movements"] += [
			movement("r1", KITCHEN, "3.0000", reason="order_cancel_restore", at="2026-10-12T17:30:00Z"),
			movement(
				"r2",
				MOZZARELLA,
				"0.2500",
				reason="order_cancel_restore",
				at="2026-10-12T17:30:00Z",
				cost="1450.0000",
			),
		]
		cancelled = load_fixture("event_order_cancelled") | {"tenant_id": TENANT_A, "seq": 20}
		cancelled["data"].update({"id": ORDER, "cancelled_at": "2026-10-12T17:29:00Z"})
		cancelled["entity"]["id"] = ORDER
		self.assertEqual(self.book(cancelled).status, "Done")
		(log,) = self.stock_log(step="order_stock_restore")
		self.assertEqual(log.status, "Done", log.message)
		self.assertEqual(
			frappe.db.get_value("Stock Entry", log.reference_name, "purpose"), "Material Receipt"
		)
		self.assertEqual((self.qty(STORES), self.qty(STORES, MOZZARELLA)), (D("20"), D("10")))

	def test_stock_trouble_never_blocks_the_invoice(self):
		self.fake_a.lists["/stock-movements"] = [movement("big", KITCHEN, "-500.0000")]
		self.assertEqual(self.book(paid()).status, "Done")
		self.assertIsNotNone(self.invoice(ORDER))
		(log,) = self.stock_log()
		self.assertEqual((log.status, log.error_class), ("Failed", "Fatal"))
		self.assertIn("ERPNext refused it", log.message)

	def test_order_without_deductions(self):
		self.fake_a.lists["/stock-movements"] = []
		self.book(paid())
		(log,) = self.stock_log()
		self.assertEqual(log.status, "Skipped")
		self.assertIn("no ingredient", log.message)

	def test_daily_mode_queues_no_stock_step(self):
		self.conn_a.db_set({"stock_deduction": DAILY})
		self.conn_a.reload()
		self.book(paid())
		self.assertEqual(self.stock_log(), [])
		self.assertEqual(self.qty(STORES), D("20"))

	def test_a_connection_never_switched_keeps_its_mode_for_every_day(self):
		self.conn_a.db_set(
			{"stock_deduction": DAILY, "stock_deduction_since": "2026-10-26", "stock_deduction_before": None}
		)
		self.conn_a.reload()
		self.assertEqual(effective_mode(self.conn_a, "2026-10-11"), DAILY)

	def test_changing_the_setting_starts_tomorrow(self):
		self.conn_a.reload()
		self.conn_a.stock_deduction = DAILY
		self.conn_a.save()
		self.assertEqual(str(self.conn_a.stock_deduction_since), str(next_business_day(self.conn_a)))
		self.assertEqual(self.conn_a.stock_deduction_before, PER_ORDER)
		# Days before the change keep the old setting; the change applies from its day on.
		self.assertEqual(effective_mode(self.conn_a, "2026-10-12"), PER_ORDER)
		self.assertEqual(effective_mode(self.conn_a, self.conn_a.stock_deduction_since), DAILY)
		self.book(paid())
		(log,) = self.stock_log()
		self.assertEqual(log.status, "Done", "an order of a per-order day is still deducted")
