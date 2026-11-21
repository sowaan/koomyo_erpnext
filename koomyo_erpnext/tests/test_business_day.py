"""Koomyo's business day: stored as received on what is booked, never worked out here, and usable in the review."""

import frappe

from koomyo_erpnext.koomyo_erpnext.report.koomyo_sales_by_payment_method_and_tax_rate import (
	koomyo_sales_by_payment_method_and_tax_rate as review,
)
from koomyo_erpnext.tests.fake_koomyo import load_fixture
from koomyo_erpnext.tests.helpers import TENANT_A
from koomyo_erpnext.tests.test_sales import SalesTestCase, refund_event
from koomyo_erpnext.tests.test_shifts import shift_event

ORDER_ID = "a8955e7a-335a-4ad0-a872-56b6d2479f80"


def fixture_order(day="2026-10-11"):
	"""The docs' order (business_date 12 Oct) paid after midnight: its trading day is the 11th."""
	event = load_fixture("event_order_paid") | {"tenant_id": TENANT_A}
	event["data"]["business_day"] = day
	return event


class TestBusinessDayStored(SalesTestCase):
	def test_the_invoice_its_payment_and_the_refund_keep_the_day_as_sent(self):
		self.assertEqual(self.book(fixture_order()).status, "Done")
		inv = self.invoice(ORDER_ID)
		self.assertEqual((str(inv.posting_date), str(inv.koomyo_business_day)), ("2026-10-12", "2026-10-11"))
		(pe,) = self.payments(inv)
		self.assertEqual(str(pe.koomyo_business_day), "2026-10-11")
		refund = refund_event(order_id=ORDER_ID, amount="724.50")
		refund["data"]["business_day"] = "2026-10-11"
		log = self.book(refund)
		self.assertEqual(log.status, "Done", log.message)
		note = frappe.get_doc("Sales Invoice", log.reference_name)
		self.assertEqual(str(note.koomyo_business_day), "2026-10-11")

	def test_an_order_without_the_field_books_as_before_and_a_bad_value_is_ignored(self):
		old = load_fixture("event_order_paid") | {"tenant_id": TENANT_A}
		self.assertNotIn("business_day", old["data"])
		self.assertEqual(self.book(old).status, "Done")
		self.assertFalse(self.invoice(ORDER_ID).koomyo_business_day)
		frappe.db.delete("Koomyo Event Log", {"connection": self.conn_a.name})
		from koomyo_erpnext.sync.common import stamp

		doc = frappe.new_doc("Sales Invoice")
		stamp(doc, self.conn_a, "x", business_day="not a date")
		self.assertFalse(doc.get("koomyo_business_day"))

	def test_the_day_is_stored_as_received_whatever_the_posting_date_says(self):
		odd = fixture_order(day="2026-09-30")  # even a day far from the posting date is kept as sent
		self.book(odd)
		self.assertEqual(str(self.invoice(ORDER_ID).koomyo_business_day), "2026-09-30")

	def test_a_shift_close_keeps_its_business_day(self):
		from koomyo_erpnext.tests.helpers import configure_sales

		configure_sales(
			self.conn_a, "_KTA", sync_shifts=1, cash_over_short_account="Miscellaneous Expenses - _KTA"
		)
		self.conn_a.reload()
		self.book(self.fixture_for_shift())
		log = self.book(shift_event(seq=70, shift_id="shift-bd", business_day="2026-10-11"))
		self.assertEqual(log.status, "Done", (log.message, (log.error or "")[-300:]))
		entry = frappe.db.get_value(
			"Journal Entry",
			{"koomyo_connection": self.conn_a.name, "koomyo_id": "shift-bd"},
			"koomyo_business_day",
		)
		self.assertEqual(str(entry), "2026-10-11")

	def fixture_for_shift(self):
		return fixture_order()


class TestReviewByBusinessDay(SalesTestCase):
	def run_report(self, **filters):
		base = {
			"company": self.conn_a.company,
			"connection": self.conn_a.name,
			"refunds": "Separate",
			"basis": "Posting date",
		}
		return review.execute({**base, **filters})

	def test_by_business_day_the_sale_counts_on_the_trading_day_not_the_calendar_date(self):
		self.book(fixture_order())  # posted 12 Oct, trading day 11 Oct
		on_posting = self.run_report(from_date="2026-10-12", to_date="2026-10-12")[1]
		self.assertEqual([r["total"] for r in on_posting], [724.5])
		self.assertEqual(self.run_report(from_date="2026-10-11", to_date="2026-10-11")[1], [])
		by_day = self.run_report(from_date="2026-10-11", to_date="2026-10-11", basis="Business day")[1]
		self.assertEqual([r["total"] for r in by_day], [724.5])
		self.assertEqual(
			self.run_report(from_date="2026-10-12", to_date="2026-10-12", basis="Business day")[1], []
		)

	def test_an_older_invoice_without_a_business_day_counts_on_its_posting_date(self):
		self.book(load_fixture("event_order_paid") | {"tenant_id": TENANT_A})
		rows = self.run_report(from_date="2026-10-12", to_date="2026-10-12", basis="Business day")[1]
		self.assertEqual([r["total"] for r in rows], [724.5])

	def test_show_each_day_adds_the_day_to_every_row(self):
		self.book(fixture_order())
		columns, rows = self.run_report(
			from_date="2026-10-01", to_date="2026-10-31", basis="Business day", by_day=1
		)
		self.assertEqual(columns[0]["fieldname"], "day")
		self.assertEqual([r["day"] for r in rows], ["2026-10-11"])
		plain_columns, plain = self.run_report(from_date="2026-10-01", to_date="2026-10-31")
		self.assertNotEqual(plain_columns[0]["fieldname"], "day")
