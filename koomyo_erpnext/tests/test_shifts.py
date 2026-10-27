"""Slice 3: shift.closed -> Journal Entry for cash over/short.

The docs' shift.closed example (fixtures/event_shift_closed.json) is the day of the docs'
sales examples: 724.50 + 1449.00 cash taken, a 724.50 cash refund, float 1000.00,
counted 2450.00, Koomyo variance -723.50 (it includes the refund).
"""

import copy
from decimal import Decimal as D

import frappe

from koomyo_erpnext.sync import events
from koomyo_erpnext.tests.fake_koomyo import load_fixture
from koomyo_erpnext.tests.helpers import TENANT_A, configure_sales, gl
from koomyo_erpnext.tests.test_sales import SalesTestCase, order_event


def shift_event(seq=60, shift_id="shift-1", **data):
	event = load_fixture("event_shift_closed")
	event.update({"id": f"evt-{shift_id}-{seq}", "seq": seq, "tenant_id": TENANT_A})
	event["data"]["id"] = shift_id
	event["data"].update(data)
	return event


class TestShifts(SalesTestCase):
	def setUp(self):
		super().setUp()
		configure_sales(
			self.conn_a, "_KTA", sync_shifts=1, cash_over_short_account="Miscellaneous Expenses - _KTA"
		)
		self.conn_a.reload()

	def entry(self, shift_id="shift-1"):
		name = frappe.db.get_value(
			"Journal Entry", {"koomyo_connection": self.conn_a.name, "koomyo_id": shift_id}
		)
		return frappe.get_doc("Journal Entry", name) if name else None

	def test_docs_day_cash_account_ends_at_counted_minus_float(self):
		# 1. the docs' sale (724.50 cash), 2. a second cash sale of 1449.00 (conventions example),
		# 3. the docs' refund of the first sale in cash, 4. the docs' shift close.
		self.assertEqual(self.book(load_fixture("event_order_paid") | {"tenant_id": TENANT_A}).status, "Done")
		second = order_event(
			seq=11,
			order_id="order-2",
			lines=[{"quantity": 2, "unit_price": "630.00", "line_total": "1260.00"}],
			payments=[("cash", "1449.00")],
			items_subtotal="1260.00",
			tax="189.00",
			total="1449.00",
			amount_due="1449.00",
			amount_paid="1449.00",
		)
		self.assertEqual(self.book(second).status, "Done")
		self.assertEqual(
			self.book(load_fixture("event_refund_created") | {"tenant_id": TENANT_A, "seq": 12}).status,
			"Done",
		)
		log = self.book(load_fixture("event_shift_closed") | {"tenant_id": TENANT_A, "seq": 13})
		self.assertEqual(log.status, "Done", log.message)

		entry = self.entry("525f08ca-00c2-408e-a6b2-8815be64ad98")
		self.assertEqual(
			(entry.docstatus, entry.voucher_type, str(entry.posting_date)), (1, "Cash Entry", "2026-10-12")
		)
		self.assertEqual(
			gl("Journal Entry", entry.name),
			{"Cash - _KTA": D("1.00"), "Miscellaneous Expenses - _KTA": D("-1.00")},
		)
		self.assertIn("-723.50", entry.user_remark)

		cash = sum(
			(
				D(str(r.debit)) - D(str(r.credit))
				for r in frappe.get_all(
					"GL Entry",
					filters={"account": "Cash - _KTA", "is_cancelled": 0, "company": self.conn_a.company},
					fields=["debit", "credit"],
				)
			),
			D(0),
		)
		self.assertEqual(cash, D("2450.00") - D("1000.00"))

	def test_only_cash_refunds_leave_the_drawer(self):
		# 1000 float + 1500 cash taken; 300 refunded in cash and 200 back to the card (amount 500, cash_amount 300).
		# The drawer should hold 2500 - 300 = 2200; 2150 counted is 50 short. Koomyo's own variance is -350.
		log = self.book(
			shift_event(
				opening_cash="1000.00",
				counted_cash="2150.00",
				expected_cash="2500.00",
				cash_variance="-350.00",
				payments_by_method=[
					{"method": "cash", "amount": "1500.00", "orders": 3, "settles_as_cash": True},
					{"method": "card", "amount": "800.00", "orders": 2, "settles_as_cash": False},
				],
				refunds={"count": 2, "amount": "500.00", "cash_amount": "300.00"},
			)
		)
		self.assertEqual(log.status, "Done", log.message)
		self.assertEqual(
			gl("Journal Entry", self.entry().name),
			{"Cash - _KTA": D("-50.00"), "Miscellaneous Expenses - _KTA": D("50.00")},
			"refunds.amount (500) would have said 150 over",
		)
		self.assertIn("300.00", self.entry().user_remark)

	def test_card_refunds_only_leave_the_drawer_alone(self):
		log = self.book(
			shift_event(
				opening_cash="100.00",
				counted_cash="1100.00",
				expected_cash="1100.00",
				cash_variance="0.00",
				payments_by_method=[
					{"method": "cash", "amount": "1000.00", "orders": 2, "settles_as_cash": True}
				],
				refunds={"count": 1, "amount": "400.00", "cash_amount": "0.00"},
			)
		)
		self.assertEqual(log.status, "Skipped", "the drawer balanced: nothing to book")
		self.assertIsNone(self.entry())

	def test_rider_cash_handed_in_at_the_till_is_part_of_expected_and_a_cash_refund_comes_off(self):
		log = self.book(
			shift_event(
				opening_cash="200.00",
				counted_cash="1200.00",
				expected_cash="1500.00",
				cash_variance="-300.00",
				rider_cash_handed_in="500.00",
				payments_by_method=[
					{"method": "cash", "amount": "800.00", "orders": 2, "settles_as_cash": True}
				],
				refunds={"count": 1, "amount": "300.00", "cash_amount": "300.00"},
			)
		)
		self.assertEqual(log.status, "Skipped", "200 + 800 + 500 - 300 = 1200 counted: balanced")

	def test_a_shift_stored_without_cash_amount_falls_back_to_amount(self):
		log = self.book(
			shift_event(
				opening_cash="0.00",
				counted_cash="900.00",
				expected_cash="1000.00",
				cash_variance="-100.00",
				payments_by_method=[
					{"method": "cash", "amount": "1000.00", "orders": 1, "settles_as_cash": True}
				],
				refunds={"count": 1, "amount": "100.00"},
			)
		)
		self.assertEqual(log.status, "Skipped", "the older shape: amount is all there is")

	def test_shortage_without_refunds(self):
		log = self.book(
			shift_event(
				opening_cash="500.00",
				counted_cash="1900.00",
				expected_cash="2000.00",
				cash_variance="-100.00",
				payments_by_method=[
					{"method": "cash", "amount": "1500.00", "orders": 3, "settles_as_cash": True},
					{"method": "card", "amount": "800.00", "orders": 2, "settles_as_cash": False},
				],
				refunds={"count": 0, "amount": "0.00"},
			)
		)
		self.assertEqual(log.status, "Done", log.message)
		self.assertEqual(
			gl("Journal Entry", self.entry().name),
			{"Cash - _KTA": D("-100.00"), "Miscellaneous Expenses - _KTA": D("100.00")},
		)

	def test_rider_cash_counts_in_expected(self):
		log = self.book(
			shift_event(
				opening_cash="0.00",
				counted_cash="1250.50",
				expected_cash="1250.00",
				cash_variance="0.50",
				rider_cash_handed_in="250.00",
				payments_by_method=[
					{"method": "cash", "amount": "1000.00", "orders": 1, "settles_as_cash": True}
				],
				refunds={"count": 0, "amount": "0.00"},
			)
		)
		self.assertEqual(log.status, "Done", log.message)
		self.assertEqual(gl("Journal Entry", self.entry().name)["Cash - _KTA"], D("0.50"))

	def test_balanced_open_and_opened(self):
		log = self.book(
			shift_event(
				opening_cash="0.00",
				counted_cash="100.00",
				expected_cash="100.00",
				cash_variance="0.00",
				payments_by_method=[
					{"method": "cash", "amount": "100.00", "orders": 1, "settles_as_cash": True}
				],
				refunds={"count": 0, "amount": "0.00"},
			)
		)
		self.assertEqual(log.status, "Skipped")
		self.assertIn("balanced", log.message)
		open_shift = shift_event(seq=61, shift_id="shift-2", status="open")
		self.assertEqual(self.book(open_shift).status, "Skipped")
		opened = load_fixture("event_shift_opened") | {"tenant_id": TENANT_A, "seq": 62, "id": "evt-opened"}
		log = self.book(opened)
		self.assertEqual(log.status, "Skipped")
		self.assertIn("float", log.message)
		self.assertEqual(frappe.get_all("Journal Entry", filters={"koomyo_connection": self.conn_a.name}), [])

	def test_figures_that_do_not_add_up(self):
		log = self.book(shift_event(counted_cash="2451.00"))
		self.assertEqual((log.status, log.error_class), ("Failed", "Fatal"))
		self.assertIn("do not add up", log.message)
		self.assertIsNone(self.entry())

	def test_replay_changes_nothing(self):
		event = load_fixture("event_shift_closed") | {"tenant_id": TENANT_A}
		self.book(event)
		self.book(copy.deepcopy(event) | {"id": "resent", "seq": 70})
		self.assertEqual(
			len(frappe.get_all("Journal Entry", filters={"koomyo_connection": self.conn_a.name})), 1
		)

	def test_drawer_account_needs_mapping_when_unclear(self):
		self.conn_a.reload()
		self.conn_a.append(
			"payment_map",
			{"koomyo_method": "cash_usd_float", "mode_of_payment": "Cash", "account": self.acc["bank"]},
		)
		self.conn_a.save()
		methods = [
			{"method": "cash", "amount": "100.00", "orders": 1, "settles_as_cash": True},
			{"method": "cash_usd_float", "amount": "50.00", "orders": 1, "settles_as_cash": True},
		]
		event = shift_event(
			opening_cash="0.00",
			counted_cash="140.00",
			expected_cash="150.00",
			cash_variance="-10.00",
			payments_by_method=methods,
			refunds={"count": 0, "amount": "0.00"},
		)
		log = self.book(event)
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn("Cash Drawer Account", log.message)

		self.conn_a.reload()
		self.conn_a.drawer_account = "Cash - _KTA"
		self.conn_a.save()
		from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry

		retry([log.name])
		events.process_pending(self.conn_a.name)
		self.assertEqual(frappe.db.get_value("Koomyo Event Log", log.name, "status"), "Done")

	def test_switch_off(self):
		self.conn_a.reload()
		self.conn_a.sync_shifts = 0
		self.conn_a.save()
		log = self.book(load_fixture("event_shift_closed") | {"tenant_id": TENANT_A})
		self.assertEqual(log.status, "Skipped")
		self.assertIn("Sync Shifts is off", log.message)
