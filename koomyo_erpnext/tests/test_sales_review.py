"""Sales by payment method and tax rate: every amount to the cent, and the rows add up to the invoices."""

from decimal import Decimal as D

import frappe

from koomyo_erpnext.koomyo_erpnext.report.koomyo_sales_by_payment_method_and_tax_rate import (
	koomyo_sales_by_payment_method_and_tax_rate as review,
)
from koomyo_erpnext.patches import fill_koomyo_tax_rate
from koomyo_erpnext.tests import test_channels as ch_tests
from koomyo_erpnext.tests.fake_koomyo import load_fixture
from koomyo_erpnext.tests.test_sales import SalesTestCase, order_event, refund_event

DOCS_ORDER = "a8955e7a-335a-4ad0-a872-56b6d2479f80"


def fixture_order():
	from koomyo_erpnext.tests.helpers import TENANT_A

	return load_fixture("event_order_paid") | {"tenant_id": TENANT_A}


class ReviewCase(SalesTestCase):
	def run_report(self, **filters):
		base = {
			"company": self.conn_a.company,
			"connection": self.conn_a.name,
			"from_date": "2026-10-01",
			"to_date": "2026-10-31",
			"refunds": "Separate",
		}
		columns, rows = review.execute({**base, **filters})
		return rows

	def by(self, rows):
		return {(r["method"], r["rate"], r["kind"]): r for r in rows}

	def split_order(self, order_id="split-1", seq=30):
		event = order_event(
			seq=seq,
			order_id=order_id,
			lines=[{"quantity": 2, "unit_price": "630.00", "line_total": "1260.00"}],
			payments=[("cash", "380.00"), ("card", "1000.00")],
			items_subtotal="1260.00",
			discount="60.00",
			tax="180.00",
			tax_rate="15.00",
			total="1380.00",
			amount_due="1380.00",
			amount_paid="1380.00",
		)
		return event

	def money(self, value):
		return D(str(value)).quantize(D("0.01"))


class TestReview(ReviewCase):
	def test_a_cash_sale_is_one_row_with_the_orders_tax_rate(self):
		self.assertEqual(self.book(fixture_order()).status, "Done")
		inv = self.invoice(DOCS_ORDER)
		self.assertEqual(inv.koomyo_tax_rate, "15.00")
		rows = self.by(self.run_report())
		row = rows[("Cash", "15%", "Sale")]
		self.assertEqual(
			(
				row["orders"],
				self.money(row["net"]),
				self.money(row["tax"]),
				self.money(row["other"]),
				self.money(row["total"]),
			),
			(1, D("630.00"), D("94.50"), D("0.00"), D("724.50")),
		)

	def test_a_split_payment_is_divided_by_what_each_method_paid_to_the_cent(self):
		self.assertEqual(self.book(self.split_order()).status, "Done")
		rows = self.by(self.run_report())
		cash, card = rows[("Cash", "15%", "Sale")], rows[("Credit Card", "15%", "Sale")]
		# 380 of 1380 is cash: net 1200 -> 330.43 + 869.57, tax 180 -> 49.57 + 130.43
		self.assertEqual((self.money(cash["net"]), self.money(card["net"])), (D("330.43"), D("869.57")))
		self.assertEqual((self.money(cash["tax"]), self.money(card["tax"])), (D("49.57"), D("130.43")))
		self.assertEqual((self.money(cash["total"]), self.money(card["total"])), (D("380.00"), D("1000.00")))
		# the order is counted once, at its largest payment
		self.assertEqual((cash["orders"], card["orders"]), (0, 1))

	def test_the_rows_always_add_up_to_the_invoices(self):
		self.book(fixture_order())
		self.book(self.split_order())
		self.book(refund_event(seq=40, order_id=DOCS_ORDER, amount="724.50", method="cash"))
		rows = self.run_report()
		invoices = frappe.get_all(
			"Sales Invoice",
			filters={"koomyo_connection": self.conn_a.name, "docstatus": 1},
			fields=["grand_total", "net_total"],
		)
		self.assertEqual(
			sum((self.money(r["total"]) for r in rows), D(0)),
			sum((self.money(i.grand_total) for i in invoices), D(0)),
		)
		for r in rows:  # on every row: net + tax + other = total
			self.assertEqual(
				self.money(r["net"]) + self.money(r["tax"]) + self.money(r["other"]),
				self.money(r["total"]),
				r,
			)

	def test_a_refund_is_its_own_row_at_the_sales_rate_and_the_option_nets_it(self):
		self.book(fixture_order())
		log = self.book(refund_event(order_id=DOCS_ORDER, amount="724.50", method="cash"))
		self.assertEqual(log.status, "Done", log.message)
		rows = self.by(self.run_report())
		refund = rows[("Cash", "15%", "Refund")]
		self.assertEqual(
			(self.money(refund["net"]), self.money(refund["tax"]), self.money(refund["total"])),
			(D("-630.00"), D("-94.50"), D("-724.50")),
		)
		self.assertEqual(self.money(rows[("Cash", "15%", "Sale")]["total"]), D("724.50"))
		netted = self.by(self.run_report(refunds="Net with sales"))
		self.assertEqual(len(netted), 1)
		self.assertEqual(self.money(netted[("Cash", "15%", "Net")]["total"]), D("0.00"))
		without = self.run_report(refunds="Leave out")
		self.assertEqual([r["kind"] for r in without], ["Sale"])

	def test_the_dates_and_the_connection_choose_what_is_counted(self):
		self.book(fixture_order())
		self.assertEqual(self.run_report(from_date="2026-11-01", to_date="2026-11-30"), [])
		self.assertEqual(len(self.run_report(connection=self.conn_b.name)), 0)

	def test_an_invoice_nobody_paid_is_not_paid_and_a_partly_paid_one_shows_the_rest(self):
		self.book(fixture_order())
		inv = self.invoice(DOCS_ORDER)
		for pe in self.payments(inv):
			pe.cancel()
		rows = self.by(self.run_report())
		self.assertIn(("Not paid", "15%", "Sale"), rows)
		self.assertEqual(self.money(rows[("Not paid", "15%", "Sale")]["total"]), D("724.50"))


class TestReviewChannels(ch_tests.ChannelCase):
	def test_a_channel_order_and_its_refund_are_their_own_lines(self):
		self.book(self.foodpanda())
		self.book(
			ch_tests.refund_event(
				seq=30, refund_id="rf-1", order_id="fp-20", amount="636.84", method="ch_foodpanda"
			)
		)
		rows = {
			(r["method"], r["rate"], r["kind"]): r
			for r in review.execute(
				{
					"company": self.conn_a.company,
					"connection": self.conn_a.name,
					"from_date": "2026-10-01",
					"to_date": "2026-10-31",
					"refunds": "Separate",
				}
			)[1]
		}
		sale = rows[("Channel: foodpanda", "16%", "Sale")]
		self.assertEqual(
			(D(str(sale["net"])), D(str(sale["tax"])), D(str(sale["total"]))),
			(D("549.00"), D("87.84"), D("636.84")),
		)
		refund = rows[("Channel: foodpanda", "16%", "Refund")]
		self.assertEqual(D(str(refund["total"])), D("-636.84"))


class TestTaxRatePatch(ReviewCase):
	def test_the_patch_fills_old_invoices_from_the_log_then_the_tax_text_and_credit_notes_from_their_sale(
		self,
	):
		self.book(fixture_order())
		self.book(refund_event(order_id=DOCS_ORDER, amount="724.50", method="cash"))
		sale = self.invoice(DOCS_ORDER)
		note = frappe.get_all("Sales Invoice", filters={"return_against": sale.name}, pluck="name")[0]
		for name in (sale.name, note):
			frappe.db.set_value("Sales Invoice", name, "koomyo_tax_rate", None)
		fill_koomyo_tax_rate.execute()
		self.assertEqual(frappe.db.get_value("Sales Invoice", sale.name, "koomyo_tax_rate"), "15.00")
		self.assertEqual(frappe.db.get_value("Sales Invoice", note, "koomyo_tax_rate"), "15.00")
		# the log is gone: the tax row's own text ("Sales tax 15.00%") gives the rate
		frappe.db.set_value("Sales Invoice", sale.name, "koomyo_tax_rate", None)
		frappe.db.delete(
			"Koomyo Event Log", {"reference_doctype": "Sales Invoice", "reference_name": sale.name}
		)
		fill_koomyo_tax_rate.execute()
		self.assertEqual(frappe.db.get_value("Sales Invoice", sale.name, "koomyo_tax_rate"), "15.00")
		# an invoice that already has a rate is never touched
		frappe.db.set_value("Sales Invoice", sale.name, "koomyo_tax_rate", "5")
		fill_koomyo_tax_rate.execute()
		self.assertEqual(frappe.db.get_value("Sales Invoice", sale.name, "koomyo_tax_rate"), "5")


class TestWorkspace(ReviewCase):
	def test_the_workspace_on_the_site_is_the_one_in_the_file_and_links_the_report(self):
		"""ERPNext re-imports a standard workspace only when the file's `modified` changes: an edit without it never
		reaches the site (the report link was missing for a day because of that)."""
		import json
		import os

		path = os.path.join(
			frappe.get_app_path("koomyo_erpnext"), "koomyo_erpnext", "workspace", "koomyo", "koomyo.json"
		)
		with open(path) as f:
			filed = json.load(f)
		self.assertEqual(
			frappe.utils.get_datetime(frappe.db.get_value("Workspace", "Koomyo", "modified")),
			frappe.utils.get_datetime(filed["modified"]),
		)
		shortcuts = frappe.get_all(
			"Workspace Shortcut", filters={"parent": "Koomyo", "type": "Report"}, pluck="link_to"
		)
		self.assertEqual(shortcuts, ["Koomyo Sales by Payment Method and Tax Rate"])
		self.assertTrue(frappe.db.exists("Report", "Koomyo Sales by Payment Method and Tax Rate"))
