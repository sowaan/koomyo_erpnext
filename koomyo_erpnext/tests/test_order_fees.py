"""POS fee (SRB branches) and packaging charge: both are inside `total`, so an order's parts must include them.

Figures follow Koomyo's docs (conventions: how the total is built). Every check is an exact Decimal to the cent.
base  = items_subtotal - discount - voucher_discount + pos_fee + (packaging_charge when packaging_taxed)
total = base + tax + channel_markup + delivery_fee + (packaging_charge when not packaging_taxed)
"""

from decimal import Decimal as D

from koomyo_erpnext.tests.helpers import gl
from koomyo_erpnext.tests.test_sales import SalesTestCase, order_event


def fee_order(order_id="fee-1", payments=None, **amounts):
	total = amounts["total"]
	event = order_event(
		order_id=order_id,
		lines=[
			{"quantity": 1, "unit_price": amounts["items_subtotal"], "line_total": amounts["items_subtotal"]}
		],
		payments=payments or [("cash", total)],
		amount_due=total,
		amount_paid=total,
		**amounts,
	)
	return event


class TestPosFeeAndPackaging(SalesTestCase):
	def test_a_pos_fee_is_taxed_like_the_items_and_booked_on_its_own_account(self):
		# items 1000.00 + fee 1.00 = base 1001.00; 15% tax 150.15; total 1151.15
		log = self.book(
			fee_order(
				items_subtotal="1000.00", pos_fee="1.00", tax="150.15", tax_rate="15.00", total="1151.15"
			)
		)
		self.assertEqual(log.status, "Done", log.message)
		inv = self.invoice("fee-1")
		self.assertMoney(inv.grand_total, "1151.15")
		self.assertEqual(
			gl("Sales Invoice", inv.name),
			{
				"Debtors - _KTA": D("1151.15"),
				"Sales - _KTA": D("-1000.00"),
				"POS Fee Income - _KTA": D("-1.00"),
				"Sales Tax - _KTA": D("-150.15"),
			},
		)

	def test_packaging_inside_the_taxed_amount_and_packaging_added_after_tax(self):
		# taxed: base 1000 + 50 = 1050, tax 157.50, total 1207.50
		taxed = fee_order(
			"pack-taxed",
			items_subtotal="1000.00",
			packaging_charge="50.00",
			packaging_taxed=True,
			tax="157.50",
			tax_rate="15.00",
			total="1207.50",
		)
		self.assertEqual(self.book(taxed).status, "Done")
		self.assertMoney(self.invoice("pack-taxed").grand_total, "1207.50")
		# not taxed: base 1000, tax 150, total 1000 + 150 + 50 = 1200.00
		after = fee_order(
			"pack-after",
			items_subtotal="1000.00",
			packaging_charge="50.00",
			packaging_taxed=False,
			tax="150.00",
			tax_rate="15.00",
			total="1200.00",
		)
		after["seq"] = 11
		after["id"] = "evt-pack-after"
		self.assertEqual(self.book(after).status, "Done")
		inv = self.invoice("pack-after")
		self.assertMoney(inv.grand_total, "1200.00")
		self.assertEqual(gl("Sales Invoice", inv.name)["Packaging Income - _KTA"], D("-50.00"))

	def test_tax_inside_the_prices_each_account_holds_its_exact_net(self):
		# items 1000.00 incl. tax + fee 10.00 incl. tax: base 1010.00, tax 15% inside = 1010 - 1010/1.15 = 131.74
		log = self.book(
			fee_order(
				"inc-1",
				items_subtotal="1000.00",
				pos_fee="10.00",
				tax="131.74",
				tax_rate="15.00",
				prices_include_tax=True,
				total="1010.00",
			)
		)
		self.assertEqual(log.status, "Done", log.message)
		inv = self.invoice("inc-1")
		self.assertMoney(inv.grand_total, "1010.00")
		posted = gl("Sales Invoice", inv.name)
		# the fee's tax share is 131.74 * 10 / 1010 = 1.30, so the fee earns 8.70 and the items 869.56 (1000.00 less 130.44 of tax)
		self.assertEqual(posted["POS Fee Income - _KTA"], D("-8.70"))
		self.assertEqual(posted["Sales - _KTA"], D("-869.56"))
		self.assertEqual(posted["Sales Tax - _KTA"], D("-131.74"))
		self.assertEqual(sum(posted.values()), D("0.00"))

	def test_an_order_that_does_not_add_up_still_books_nothing(self):
		log = self.book(
			fee_order(
				"bad-1",
				items_subtotal="1000.00",
				pos_fee="1.00",
				tax="150.00",
				tax_rate="15.00",
				total="1151.15",
			)
		)
		self.assertEqual((log.status, log.error_class), ("Failed", "Fatal"))
		self.assertIsNone(self.invoice("bad-1"))

	def test_without_the_account_the_order_waits_for_a_mapping_and_nothing_is_half_booked(self):
		self.conn_a.pos_fee_account = None
		self.conn_a.save()
		log = self.book(
			fee_order(
				"nomap-1",
				items_subtotal="1000.00",
				pos_fee="1.00",
				tax="150.15",
				tax_rate="15.00",
				total="1151.15",
			)
		)
		self.assertEqual((log.status, log.error_class), ("Failed", "NeedsMapping"))
		self.assertIn("POS Fee Account", log.message)
		self.assertIsNone(self.invoice("nomap-1"))

	def test_an_older_koomyo_without_the_fields_books_as_before(self):
		event = fee_order("old-1", items_subtotal="1000.00", tax="150.00", tax_rate="15.00", total="1150.00")
		for key in ("pos_fee", "packaging_charge", "packaging_taxed"):
			event["data"]["amounts"].pop(key, None)
		self.assertEqual(self.book(event).status, "Done")
		self.assertMoney(self.invoice("old-1").grand_total, "1150.00")

	def test_a_refund_of_an_order_with_a_fee_reverses_the_fee_too(self):
		self.book(
			fee_order(
				items_subtotal="1000.00", pos_fee="1.00", tax="150.15", tax_rate="15.00", total="1151.15"
			)
		)
		from koomyo_erpnext.tests.test_sales import refund_event

		log = self.book(refund_event(order_id="fee-1", amount="1151.15"))
		self.assertEqual(log.status, "Done", log.message)
		self.assertEqual(gl("Sales Invoice", log.reference_name)["POS Fee Income - _KTA"], D("1.00"))

	def test_the_srb_number_and_link_are_kept_and_unsafe_text_is_not(self):
		event = fee_order("srb-1", items_subtotal="1000.00", tax="150.00", tax_rate="15.00", total="1150.00")
		event["data"]["srb"] = {
			"invoice_id": "ORD-1",
			"number": "SRB-123",
			"qr_link": "https://srb.example/verify/abc",
			"status": "sent",
			"returns": [],
		}
		self.assertEqual(self.book(event).status, "Done")
		inv = self.invoice("srb-1")
		self.assertEqual((inv.koomyo_srb_number, inv.koomyo_srb_status), ("SRB-123", "sent"))
		self.assertEqual(inv.koomyo_srb_link, "https://srb.example/verify/abc")
		bad = fee_order("srb-2", items_subtotal="1000.00", tax="150.00", tax_rate="15.00", total="1150.00")
		bad["seq"] = 12
		bad["id"] = "evt-srb-2"
		bad["data"]["srb"] = {
			"number": None,
			"qr_link": "javascript:alert(1)",
			"status": "pending",
			"returns": [],
		}
		self.assertEqual(self.book(bad).status, "Done")
		inv = self.invoice("srb-2")
		self.assertFalse(inv.koomyo_srb_link, "a link that is not https is not stored")
		self.assertEqual(inv.koomyo_srb_status, "pending")
