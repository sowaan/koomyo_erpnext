"""Cash on delivery collected short (cod_short) and extra cash kept by the rider (cod_extra).

The payment is what the rider handed in for a short collection, and exactly the amount due for extra cash.
Null (or absent) is nothing special. Replays change nothing.
"""

import copy
from decimal import Decimal as D

import frappe

from koomyo_erpnext.tests.helpers import gl
from koomyo_erpnext.tests.test_sales import SalesTestCase, order_event

SHORT_ACCOUNT = "Miscellaneous Expenses - _KTA"


def cod_event(collected="90.00", due="100.00", order_id="order-cod", short=None, extra=None, **keys):
	event = order_event(
		seq=10,
		order_id=order_id,
		lines=[{"quantity": 1, "unit_price": "100.00", "line_total": "100.00"}],
		payments=[("cash", collected)],
		items_subtotal="100.00",
		total="100.00",
		amount_due=due,
		amount_paid=collected,
	)
	event["data"].update({"cod_short": short, "cod_extra": extra, **keys})
	return event


def short_block(**changes):
	block = {
		"expected": "100.00",
		"collected": "90.00",
		"shortfall": "10.00",
		"reason": "customer_short",
		"allowed_by": "permission",
		"recorded_at": "2026-10-12T16:53:19Z",
	}
	return {**block, **changes}


def extra_block(**changes):
	return {**{"amount": "5.00", "reason": "no_change", "recorded_at": "2026-10-12T16:53:19Z"}, **changes}


class CodCase(SalesTestCase):
	def setUp(self):
		super().setUp()
		from koomyo_erpnext.auto_setup import add_mode_account

		mode = frappe.get_doc("Mode of Payment", "Cash")
		if not [r for r in mode.accounts if r.company == self.conn_a.company]:
			add_mode_account("Cash", self.conn_a.company, "Cash - _KTA")
			self.added_mode_account = True
		self.conn_a.reload()
		self.conn_a.cod_shortfall_account = SHORT_ACCOUNT
		self.conn_a.cod_extra_account = self.conn_a.income_account
		self.conn_a.save()

	def tearDown(self):
		if getattr(self, "added_mode_account", False):
			frappe.db.delete("Mode of Payment Account", {"parent": "Cash", "company": self.conn_a.company})
			frappe.db.commit()
		super().tearDown()

	def jes(self, prefix):
		return frappe.get_all(
			"Journal Entry",
			filters={
				"koomyo_connection": self.conn_a.name,
				"koomyo_id": ("like", f"{prefix}:%"),
				"docstatus": 1,
			},
			pluck="name",
		)

	def outstanding(self, order_id="order-cod"):
		return D(str(frappe.db.get_value("Sales Invoice", self.invoice(order_id).name, "outstanding_amount")))


class TestCodShort(CodCase):
	def test_the_payment_is_what_was_collected_and_the_shortfall_is_written_off(self):
		log = self.book(cod_event(short=short_block()))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		invoice = self.invoice("order-cod")
		(pe,) = self.payments(invoice)
		self.assertEqual(pe.paid_amount, 90)
		self.assertEqual(self.outstanding(), D("0"))
		(name,) = self.jes("cod-short")
		self.assertEqual(
			gl("Journal Entry", name), {SHORT_ACCOUNT: D("10.00"), "Debtors - _KTA": D("-10.00")}
		)
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice.name, "status"), "Paid")

	def test_without_the_account_the_sale_books_and_the_row_says_what_to_set(self):
		self.conn_a.reload()
		self.conn_a.cod_shortfall_account = None
		self.conn_a.save()
		log = self.book(cod_event(short=short_block()))
		self.assertEqual(log.status, "Done")
		self.assertIn("COD Shortfall Account", log.message)
		self.assertEqual(self.outstanding(), D("10"))
		self.assertEqual(self.jes("cod-short"), [])

	def test_a_replay_changes_nothing(self):
		event = cod_event(short=short_block())
		self.book(event)
		again = copy.deepcopy(event)
		again.update({"id": "evt-again", "seq": 11})
		self.book(again)
		self.assertEqual(len(self.jes("cod-short")), 1)
		self.assertEqual(len(self.payments(self.invoice("order-cod"))), 1)

	def test_figures_that_do_not_add_up_book_nothing(self):
		log = self.book(cod_event(short=short_block(shortfall="9.00")))
		self.assertEqual((log.status, log.error_class), ("Failed", "Fatal"))
		self.assertIn("do not add up", log.message)
		self.assertFalse(frappe.db.exists("Sales Invoice", {"koomyo_id": "order-cod"}))

	def test_a_block_that_disagrees_with_the_order_amounts_books_nothing(self):
		log = self.book(cod_event(short=short_block(expected="120.00", collected="110.00")))
		self.assertEqual(log.status, "Failed")
		self.assertIn("the order's amounts say", log.message)

	def test_null_and_absent_are_nothing_special(self):
		event = cod_event(collected="100.00", due="100.00")
		self.assertEqual(self.book(event).status, "Done")
		self.assertEqual(self.outstanding(), D("0"))
		absent = cod_event(collected="100.00", order_id="order-plain")
		del absent["data"]["cod_short"], absent["data"]["cod_extra"]
		absent.update({"id": "evt-plain", "seq": 12})
		self.assertEqual(self.book(absent).status, "Done")
		self.assertEqual(self.jes("cod-short") + self.jes("cod-extra"), [])

	def test_a_block_of_the_wrong_kind_is_refused(self):
		log = self.book(cod_event(short="yes"))
		self.assertEqual(log.status, "Failed")
		self.assertIn("not an object or null", log.message)


class TestCodExtra(CodCase):
	def test_the_extra_is_income_apart_from_the_sale_and_held_in_cash(self):
		log = self.book(cod_event(collected="100.00", extra=extra_block()))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		invoice = self.invoice("order-cod")
		self.assertEqual(invoice.grand_total, 100)
		(pe,) = self.payments(invoice)
		self.assertEqual(pe.paid_amount, 100, "the payment is exactly the amount due")
		(name,) = self.jes("cod-extra")
		self.assertEqual(
			gl("Journal Entry", name),
			{"Cash - _KTA": D("5.00"), self.conn_a.income_account: D("-5.00")},
		)

	def test_without_the_account_it_is_only_noted(self):
		self.conn_a.reload()
		self.conn_a.cod_extra_account = None
		self.conn_a.save()
		log = self.book(cod_event(collected="100.00", extra=extra_block(reason="tip")))
		self.assertEqual(log.status, "Done")
		self.assertIn("Rider Extra Cash Account", log.message)
		self.assertEqual(self.jes("cod-extra"), [])
		notes = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Sales Invoice", "reference_name": self.invoice("order-cod").name},
			pluck="content",
		)
		self.assertTrue([n for n in notes if "Extra cash kept by the rider" in n])

	def test_short_and_extra_never_come_together_but_each_is_booked_alone(self):
		log = self.book(cod_event(short=short_block(), extra=extra_block(amount="2.00")))
		self.assertEqual(log.status, "Done")
		self.assertEqual((len(self.jes("cod-short")), len(self.jes("cod-extra"))), (1, 1))

	def test_the_extra_is_not_in_the_refund_limit(self):
		from koomyo_erpnext.tests.test_sales import refund_event

		self.book(cod_event(collected="100.00", extra=extra_block()))
		log = self.book(refund_event(order_id="order-cod", amount="100.00", method="cash"))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
