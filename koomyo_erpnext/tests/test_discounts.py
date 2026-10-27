"""Section 1: discounts on sales (docs.koomyo.com/guides/discounts).

Each line is posted at its list price with the discount shown, the discount on the account of its
source; Koomyo's tax is taken as sent. Money assertions compare exact Decimals to the cent.
"""

from decimal import Decimal as D

import frappe

from koomyo_erpnext.sync.common import find_existing
from koomyo_erpnext.tests.helpers import TENANT_A, gl
from koomyo_erpnext.tests.test_sales import SalesTestCase, order_event, refund_event

PIZZA = {"item_id": "menu-1", "name": "Pizza", "size": "L", "size_id": "size-L"}


def discounted(list_price, net, *parts, quantity=1, **extra):
	"""A line the way Koomyo sends one: list/net prices and the per-source discount parts."""
	list_total = D(list_price) * quantity
	return {
		**PIZZA,
		"quantity": quantity,
		"unit_price": list_price,
		"list_price": list_price,
		"list_total": str(list_total),
		"line_total": net,
		"net_price": net,
		"discount_amount": str(list_total - D(net)),
		"discount_source": parts[0][0] if parts else None,
		"discounts": [{"source": s, "amount": a} for s, a in parts],
		**extra,
	}


def order(lines, subtotal, discount_total, tax="0.00", **kw):
	total = D(subtotal) - D(kw.get("discount", "0.00")) - D(kw.get("voucher_discount", "0.00"))
	due = total + D(tax)
	return order_event(
		lines=lines,
		payments=[("cash", str(due))],
		items_subtotal=subtotal,
		items_list_total=kw.pop("items_list_total", None),
		discount_total=discount_total,
		tax=tax,
		total=str(due),
		amount_due=str(due),
		amount_paid=str(due),
		**kw,
	)


class TestDiscountPosting(SalesTestCase):
	def booked(self, event):
		log = self.book(event)
		self.assertEqual(log.status, "Done", log.message)
		return self.invoice(event["data"]["id"])

	def test_mixed_line_one_rate_per_unit_and_sources_on_their_accounts(self):
		# 1099 = 1000 pizza + 99 extra; promotion 600 + slice reward 400 -> pays 99.
		line = discounted("1099.00", "99.00", ("promotion", "600.00"), ("slice_reward", "400.00"))
		event = order([line], "99.00", "1000.00", tax="14.85")
		inv = self.booked(event)
		(row,) = inv.items
		self.assertMoney(row.price_list_rate, "1099.00")
		self.assertMoney(row.discount_amount, "1000.00")
		self.assertMoney(row.rate, "99.00")
		self.assertMoney(inv.grand_total, "113.85")
		# ERPNext posts the whole 1000.00 on the larger source; a Journal Entry moves 400.00 over.
		self.assertEqual(
			gl("Sales Invoice", inv.name),
			{
				"Debtors - _KTA": D("113.85"),
				"Sales - _KTA": D("-1099.00"),
				"Sales Promotions - _KTA": D("1000.00"),
				"Sales Tax - _KTA": D("-14.85"),
			},
		)
		je = frappe.get_doc(
			"Journal Entry", find_existing("Journal Entry", self.conn_a, f"discount:{event['data']['id']}")
		)
		self.assertEqual(
			gl("Journal Entry", je.name),
			{"Sales Promotions - _KTA": D("-400.00"), "Loyalty Reward Discount - _KTA": D("400.00")},
		)

	def test_free_slice_pizza_discounts_the_pizza_only_extras_stay_paid(self):
		# Pizza 1000 free via a slice reward; two paid extras 99 each stay at full price.
		line = discounted("1198.00", "198.00", ("slice_reward", "1000.00"))
		inv = self.booked(order([line], "198.00", "1000.00"))
		(row,) = inv.items
		self.assertMoney(row.discount_amount, "1000.00")
		self.assertMoney(inv.grand_total, "198.00")
		self.assertEqual(gl("Sales Invoice", inv.name)["Loyalty Reward Discount - _KTA"], D("1000.00"))
		self.assertFalse(frappe.db.exists("Journal Entry", {"koomyo_id": ("like", "discount:%")}))

	def test_fully_free_line_books_at_zero_with_the_full_discount(self):
		line = discounted("500.00", "0.00", ("gift_item", "500.00"))
		inv = self.booked(order([line], "0.00", "500.00"))
		(row,) = inv.items
		self.assertMoney(row.rate, "0.00")
		self.assertMoney(inv.grand_total, "0.00")
		self.assertEqual(
			gl("Sales Invoice", inv.name),
			{"Sales - _KTA": D("-500.00"), "Gift Items Discount - _KTA": D("500.00")},
		)

	def test_quantity_three_with_uneven_net_still_adds_up_to_the_cent(self):
		# 3 x 400.00 list, net 1000.00 -> 333.33 x 2 + 333.34
		line = discounted("400.00", "1000.00", ("manual", "200.00"), quantity=3)
		inv = self.booked(order([line], "1000.00", "200.00"))
		self.assertEqual(
			sorted((i.qty, D(str(i.rate))) for i in inv.items), [(1, D("333.34")), (2, D("333.33"))]
		)
		self.assertMoney(inv.net_total, "1000.00")
		self.assertMoney(inv.grand_total, "1000.00")

	def test_order_level_voucher_and_manual_discount(self):
		line = discounted("1000.00", "1000.00", quantity=1)
		event = order(
			[line],
			"1000.00",
			"150.00",
			tax="127.50",
			voucher_discount="100.00",
			discount="50.00",
		)
		event["data"]["order_discounts"] = [
			{"source": "voucher", "amount": "100.00"},
			{"source": "manual", "amount": "50.00"},
		]
		inv = self.booked(event)
		self.assertMoney(inv.discount_amount, "150.00")
		self.assertEqual(inv.additional_discount_account, "Voucher Discount - _KTA")
		# the 50.00 staff discount is moved from the voucher account to its own
		self.assertEqual(
			gl(
				"Journal Entry",
				find_existing("Journal Entry", self.conn_a, f"discount:{event['data']['id']}"),
			),
			{"Voucher Discount - _KTA": D("-50.00"), "Staff Discounts - _KTA": D("50.00")},
		)

	def test_tax_is_koomyos_number_not_recalculated(self):
		line = discounted("1000.00", "800.00", ("promotion", "200.00"))
		inv = self.booked(order([line], "800.00", "200.00", tax="123.45"))
		self.assertMoney(inv.total_taxes_and_charges, "123.45")
		self.assertMoney(inv.grand_total, "923.45")

	def test_api_channel_is_an_ordinary_sale_and_does_not_move_stock(self):
		line = discounted("1000.00", "900.00", ("promotion", "100.00"))
		event = order([line], "900.00", "100.00")
		event["data"]["channel"] = "api"
		inv = self.booked(event)
		self.assertEqual(inv.update_stock, 0)
		self.assertMoney(inv.grand_total, "900.00")

	def test_gift_item_without_a_recorded_price_is_not_guessed(self):
		line = {
			**PIZZA,
			"quantity": 1,
			"unit_price": "0.00",
			"line_total": "0.00",
			"net_price": "0.00",
			"list_price": None,
			"list_total": None,
			"discount_amount": None,
			"discounts": [],
		}
		inv = self.booked(order([line], "0.00", "0.00"))
		(row,) = inv.items
		self.assertMoney(row.rate, "0.00")
		self.assertFalse(row.discount_account)
		self.assertIn("not recorded", row.description)

	def test_discount_totals_that_do_not_add_up_book_nothing(self):
		line = discounted("1000.00", "900.00", ("promotion", "100.00"))
		event = order([line], "900.00", "100.00", items_list_total="1000.00")
		event["data"]["amounts"]["discount_total"] = "50.00"
		log = self.book(event)
		self.assertEqual(log.status, "Failed")
		self.assertFalse(self.invoice(event["data"]["id"]))

	def test_unmapped_source_names_the_row_to_add(self):
		self.conn_a.reload()
		self.conn_a.set("discount_map", [r for r in self.conn_a.discount_map if r.source != "promotion"])
		self.conn_a.save(ignore_permissions=True)
		line = discounted("1000.00", "900.00", ("promotion", "100.00"))
		log = self.book(order([line], "900.00", "100.00"))
		self.assertEqual(log.status, "Failed")
		self.assertIn("Discount Map", log.message)
		self.assertIn("promotion", log.message)

	def test_discount_accounting_off_is_a_mapping_problem_not_a_wrong_posting(self):
		frappe.db.set_single_value("Selling Settings", "enable_discount_accounting", 0)
		try:
			line = discounted("1000.00", "900.00", ("promotion", "100.00"))
			log = self.book(order([line], "900.00", "100.00"))
			self.assertEqual(log.status, "Failed")
			self.assertIn("Discount Accounting", log.message)
		finally:
			frappe.db.set_single_value("Selling Settings", "enable_discount_accounting", 1)

	def test_discount_map_validates_company_and_duplicates(self):
		self.conn_a.reload()
		self.conn_a.append("discount_map", {"source": "promotion", "account": "Sales - _KTB"})
		self.assertRaises(frappe.ValidationError, self.conn_a.save, ignore_permissions=True)


class TestDiscountRefunds(SalesTestCase):
	def test_full_refund_mirrors_the_discounts_including_the_reclassification(self):
		line = discounted("1099.00", "99.00", ("promotion", "600.00"), ("slice_reward", "400.00"))
		event = order([line], "99.00", "1000.00")
		self.assertEqual(self.book(event).status, "Done")
		inv = self.invoice(event["data"]["id"])
		refund = refund_event(order_id=event["data"]["id"], amount=str(inv.grand_total))
		log = self.book(refund)
		self.assertEqual(log.status, "Done", log.message)
		note = frappe.get_doc("Sales Invoice", log.reference_name)
		self.assertEqual(
			gl("Sales Invoice", note.name),
			{
				"Debtors - _KTA": D("-99.00"),
				"Sales - _KTA": D("1099.00"),
				"Sales Promotions - _KTA": D("-1000.00"),
			},
		)
		self.assertTrue(
			find_existing("Journal Entry", self.conn_a, f"discount-refund:{refund['data']['id']}")
		)
		# net effect after sale, reclass, credit note and its reversal: every discount account is back to zero
		for account in ("Sales Promotions - _KTA", "Loyalty Reward Discount - _KTA"):
			balance = frappe.db.sql(
				"select coalesce(sum(debit-credit),0) from `tabGL Entry` where account=%s and is_cancelled=0 and voucher_no in %s",
				(
					account,
					(
						inv.name,
						note.name,
						find_existing("Journal Entry", self.conn_a, f"discount:{event['data']['id']}"),
						find_existing(
							"Journal Entry", self.conn_a, f"discount-refund:{refund['data']['id']}"
						),
					),
				),
			)[0][0]
			self.assertEqual(D(str(balance)), D("0"), account)
