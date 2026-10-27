"""Slice 2: order.paid -> Sales Invoice + Payment Entries; refund.created -> Credit Note.

Sample payloads start from the docs examples (fixtures/event_order_paid.json,
event_refund_created.json) and the worked numbers on docs.koomyo.com/conventions.
Every money assertion compares exact Decimals to the cent.
"""

import copy
from decimal import Decimal as D

import frappe

from koomyo_erpnext.sync import events
from koomyo_erpnext.tests.fake_koomyo import load_fixture
from koomyo_erpnext.tests.helpers import (
	COMPANY_A,
	COMPANY_B,
	SECRET_A,
	TENANT_A,
	TENANT_B,
	KoomyoIntegrationTestCase,
	configure_sales,
	event_body,
	gl,
	post_webhook,
	sign,
)

BRANCH = "90000000-0000-4000-8000-000000000001"
ZERO = {
	"items_subtotal": "0.00",
	"discount": "0.00",
	"voucher_discount": "0.00",
	"channel_markup": "0.00",
	"delivery_fee": "0.00",
	"tax": "0.00",
	"tax_rate": "15.00",
	"prices_include_tax": False,
	"total": "0.00",
	"payment_discount": "0.00",
	"payment_discount_pre_tax": True,
	"amount_due": "0.00",
	"amount_paid": "0.00",
	"amount_refunded": "0.00",
}


def price_fields(row, given):
	"""Fill the list/net price fields of a test line the way Koomyo sends them (options inside the prices)."""
	if "list_price" in given:
		return
	qty = row["quantity"]
	net = D(row["line_total"])
	unit = D(row["unit_price"])
	row["list_price"] = row["unit_price"]
	row["list_total"] = str(unit * qty)
	row["net_price"] = row["line_total"]
	row["discount_amount"] = str(unit * qty - net)
	row["discount_source"] = None
	row["discounts"] = []
	if unit * qty != net:
		row["discount_source"] = "manual"
		row["discounts"] = [{"source": "manual", "amount": row["discount_amount"]}]


def order_event(seq=10, order_id="order-1", lines=None, payments=None, tenant=TENANT_A, **amounts):
	"""The docs' order.paid example with other lines, payments and amounts."""
	event = load_fixture("event_order_paid")
	event.update({"id": f"evt-{order_id}-{seq}", "seq": seq, "tenant_id": tenant})
	order = event["data"]
	order["id"] = order_id
	event["entity"]["id"] = order_id
	if lines is not None:
		template = order["items"][0]
		order["items"] = []
		for i, line in enumerate(lines):
			row = copy.deepcopy(template)
			row.update({"id": f"{order_id}-line-{i}", "modifiers": [], "promo_discount": "0.00"})
			row.update(line)
			price_fields(row, line)
			order["items"].append(row)
	if payments is not None:
		order["payment"]["payments"] = [
			{
				"id": f"{order_id}-pay-{i}",
				"method": m,
				"amount": a,
				"reference": None,
				"created_at": "2026-10-12T16:53:19Z",
				"shift_id": None,
			}
			for i, (m, a) in enumerate(payments)
		]
	if amounts:
		order["amounts"] = {**ZERO, **amounts}
	return event


def refund_event(
	seq=20, refund_id="refund-1", order_id="order-1", amount="724.50", method="cash", tenant=TENANT_A
):
	event = load_fixture("event_refund_created")
	event.update({"id": f"evt-{refund_id}", "seq": seq, "tenant_id": tenant})
	refund = event["data"]
	refund.update({"id": refund_id, "order_id": order_id, "amount": amount, "payment_method": method})
	refund["order"]["id"] = order_id
	return event


class SalesTestCase(KoomyoIntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.acc = configure_sales(self.conn_a, "_KTA")
		self.conn_a.reload()

	def book(self, event, connection=None, fake=None):
		"""Store the event as Koomyo's feed would deliver it, then process it."""
		connection = connection or self.conn_a
		(fake or self.fake_a).events.append(event)
		with self.koomyo():
			events.catch_up(connection.name)
			events.process_pending(connection.name)
		return frappe.get_all(
			"Koomyo Event Log", filters={"connection": connection.name, "event_id": event["id"]}, fields=["*"]
		)[0]

	def invoice(self, order_id="order-1", connection=None):
		name = frappe.db.get_value(
			"Sales Invoice", {"koomyo_connection": (connection or self.conn_a).name, "koomyo_id": order_id}
		)
		return frappe.get_doc("Sales Invoice", name) if name else None

	def payments(self, invoice):
		names = frappe.get_all(
			"Payment Entry Reference",
			filters={"reference_name": invoice.name, "docstatus": 1},
			pluck="parent",
		)
		return [frappe.get_doc("Payment Entry", n) for n in sorted(set(names))]

	def assertMoney(self, value, expected):
		self.assertEqual(D(str(value)).quantize(D("0.01")), D(expected))


class TestSalesInvoice(SalesTestCase):
	def test_docs_example_cash_sale(self):
		log = self.book(load_fixture("event_order_paid") | {"tenant_id": TENANT_A})
		self.assertEqual(log.status, "Done", log.message)
		inv = self.invoice("a8955e7a-335a-4ad0-a872-56b6d2479f80")
		self.assertEqual((inv.docstatus, inv.company, inv.customer), (1, COMPANY_A, "_Koomyo Walk-in"))
		self.assertEqual(str(inv.posting_date), "2026-10-12")
		self.assertEqual(str(inv.posting_time), "21:53:19")  # 16:53:19Z in Asia/Karachi
		self.assertEqual(inv.po_no, "ORD-PFB-20261012-0002")
		self.assertEqual(inv.koomyo_branch, BRANCH)
		self.assertMoney(inv.net_total, "630.00")
		self.assertMoney(inv.total_taxes_and_charges, "94.50")
		self.assertMoney(inv.grand_total, "724.50")
		self.assertMoney(inv.outstanding_amount, "0.00")
		self.assertEqual(inv.update_stock, 0)
		self.assertEqual(
			gl("Sales Invoice", inv.name),
			{
				"Debtors - _KTA": D("724.50"),
				"Sales - _KTA": D("-900.00"),
				"Sales Promotions - _KTA": D("270.00"),
				"Sales Tax - _KTA": D("-94.50"),
			},
		)
		# the line is posted at its list price with the discount shown, net = Koomyo's net price
		(row,) = inv.items
		self.assertMoney(row.price_list_rate, "900.00")
		self.assertMoney(row.discount_amount, "270.00")
		self.assertMoney(row.rate, "630.00")
		self.assertEqual(row.discount_account, "Sales Promotions - _KTA")
		(pe,) = self.payments(inv)
		self.assertEqual((pe.mode_of_payment, pe.koomyo_id), ("Cash", "41a8f2dc-3156-4d01-909c-b0d6244b537c"))
		self.assertEqual(
			gl("Payment Entry", pe.name), {"Cash - _KTA": D("724.50"), "Debtors - _KTA": D("-724.50")}
		)
		self.assertEqual(log.reference_name, inv.name)

	def test_split_payment_discount_and_tax(self):
		# 2 x 630.00, manager discount 60.00 -> base 1200.00, tax 15% 180.00, total 1380.00;
		# paid 380.00 cash + 1000.00 card (2% card fee).
		event = order_event(
			lines=[{"quantity": 2, "unit_price": "630.00", "line_total": "1260.00"}],
			payments=[("cash", "380.00"), ("card", "1000.00")],
			items_subtotal="1260.00",
			discount="60.00",
			tax="180.00",
			total="1380.00",
			amount_due="1380.00",
			amount_paid="1380.00",
		)
		self.assertEqual(self.book(event).status, "Done")
		inv = self.invoice()
		self.assertMoney(inv.total, "1260.00")
		self.assertMoney(inv.discount_amount, "60.00")
		self.assertMoney(inv.items[0].rate, "630.00")  # the discount is not hidden in the rate
		self.assertMoney(inv.net_total, "1200.00")
		self.assertMoney(inv.grand_total, "1380.00")
		self.assertMoney(inv.outstanding_amount, "0.00")
		self.assertEqual(
			gl("Sales Invoice", inv.name),
			{
				"Debtors - _KTA": D("1380.00"),
				"Sales - _KTA": D("-1260.00"),
				"Staff Discounts - _KTA": D("60.00"),
				"Sales Tax - _KTA": D("-180.00"),
			},
		)
		cash, card = sorted(self.payments(inv), key=lambda p: p.paid_amount)
		self.assertEqual(
			gl("Payment Entry", cash.name), {"Cash - _KTA": D("380.00"), "Debtors - _KTA": D("-380.00")}
		)
		self.assertEqual(
			gl("Payment Entry", card.name),
			{
				"Card Clearing - _KTA": D("980.00"),
				"Commission on Sales - _KTA": D("20.00"),
				"Debtors - _KTA": D("-1000.00"),
			},
		)

	def test_tax_inclusive_prices_exact_to_the_cent(self):
		# 999.99 with 15% inside: tax = 999.99 - 999.99/1.15 = 130.43, revenue 869.56.
		event = order_event(
			lines=[{"quantity": 1, "unit_price": "999.99", "line_total": "999.99"}],
			payments=[("cash", "999.99")],
			items_subtotal="999.99",
			tax="130.43",
			prices_include_tax=True,
			total="999.99",
			amount_due="999.99",
			amount_paid="999.99",
		)
		self.assertEqual(self.book(event).status, "Done")
		inv = self.invoice()
		self.assertMoney(inv.grand_total, "999.99")
		self.assertEqual(
			gl("Sales Invoice", inv.name),
			{
				"Debtors - _KTA": D("999.99"),
				"Sales - _KTA": D("-869.56"),
				"Sales Tax - _KTA": D("-130.43"),
			},
		)

	def test_payment_discount_before_tax(self):
		# Conventions: 1000.00 items, 100.00 payment discount before tax -> tax 135.00, total 1035.00.
		event = order_event(
			lines=[{"quantity": 1, "unit_price": "1000.00", "line_total": "1000.00"}],
			payments=[("cash", "1035.00")],
			items_subtotal="1000.00",
			tax="135.00",
			total="1035.00",
			payment_discount="100.00",
			payment_discount_pre_tax=True,
			amount_due="1035.00",
			amount_paid="1035.00",
		)
		self.assertEqual(self.book(event).status, "Done")
		inv = self.invoice()
		self.assertMoney(inv.grand_total, "1035.00")
		self.assertEqual(
			gl("Sales Invoice", inv.name),
			{
				"Debtors - _KTA": D("1035.00"),
				"Sales - _KTA": D("-1000.00"),
				"Sales Tax - _KTA": D("-135.00"),
				"Sales Expenses - _KTA": D("100.00"),
			},
		)

	def test_payment_discount_after_tax(self):
		# Conventions: tax 150.00 on 1000.00, total 1150.00, discount 100.00 after tax -> 1050.00 due.
		event = order_event(
			lines=[{"quantity": 1, "unit_price": "1000.00", "line_total": "1000.00"}],
			payments=[("cash", "1050.00")],
			items_subtotal="1000.00",
			tax="150.00",
			total="1150.00",
			payment_discount="100.00",
			payment_discount_pre_tax=False,
			amount_due="1050.00",
			amount_paid="1050.00",
		)
		self.assertEqual(self.book(event).status, "Done")
		self.assertMoney(self.invoice().grand_total, "1050.00")

	def test_card_surcharge_delivery_and_markup(self):
		# 1000.00 + tax 150.00 + markup 50.00 + delivery 100.00 = 1300.00; a 20.00 card surcharge after tax.
		event = order_event(
			lines=[{"quantity": 1, "unit_price": "1000.00", "line_total": "1000.00"}],
			payments=[("card", "1320.00")],
			items_subtotal="1000.00",
			tax="150.00",
			channel_markup="50.00",
			delivery_fee="100.00",
			total="1300.00",
			payment_discount="-20.00",
			payment_discount_pre_tax=False,
			amount_due="1320.00",
			amount_paid="1320.00",
		)
		self.assertEqual(self.book(event).status, "Done")
		inv = self.invoice()
		self.assertMoney(inv.grand_total, "1320.00")
		self.assertEqual(
			gl("Sales Invoice", inv.name),
			{
				"Debtors - _KTA": D("1320.00"),
				"Sales - _KTA": D("-1000.00"),
				"Sales Tax - _KTA": D("-150.00"),
				"Channel Markup Income - _KTA": D("-50.00"),
				"Delivery Income - _KTA": D("-100.00"),
				"Sales Expenses - _KTA": D("-20.00"),
			},
		)

	def test_modifiers_voided_lines_and_menu_items(self):
		event = order_event(
			lines=[
				{
					"item_id": "menu-1",
					"name": "Pizza",
					"size": "L",
					"size_id": "size-L",
					"quantity": 2,
					"unit_price": "680.00",
					"line_total": "1360.00",
					"modifiers": [{"modifier_id": "m1", "name": "Extra cheese", "price": "50.00"}],
				},
				{
					"item_id": "menu-2",
					"name": "Cola",
					"size": None,
					"size_id": None,
					"quantity": 1,
					"unit_price": "100.00",
					"line_total": "100.00",
					"voided": True,
				},
				{
					"item_id": "menu-1",
					"name": "Pizza",
					"size": "S",
					"size_id": "size-S",
					"quantity": 1,
					"unit_price": "400.00",
					"line_total": "400.00",
				},
			],
			payments=[("cash", "2139.00")],
			items_subtotal="1760.00",
			tax="264.00",
			total="2024.00",
			delivery_fee="115.00",
			amount_due="2139.00",
			amount_paid="2139.00",
		)
		self.assertEqual(self.book(event).status, "Done")
		inv = self.invoice()
		self.assertEqual(
			[(i.qty, D(str(i.rate)).quantize(D("0.01"))) for i in inv.items],
			[(2, D("680.00")), (1, D("400.00"))],
		)
		self.assertIn("Extra cheese", inv.items[0].description, "the line itself names its modifiers")
		large, small = inv.items
		self.assertNotEqual(large.item_code, small.item_code, "each size is its own variant")
		template = frappe.db.get_value("Item", large.item_code, "variant_of")
		self.assertEqual(frappe.db.get_value("Item", small.item_code, "variant_of"), template)
		item = frappe.get_doc("Item", template)
		self.assertEqual(
			(item.koomyo_connection, item.koomyo_id, item.is_stock_item, item.has_variants),
			(self.conn_a.name, "menu-1", 0, 1),
		)
		self.assertEqual(frappe.db.get_value("Item", large.item_code, "koomyo_id"), "size-L")
		self.assertMoney(inv.grand_total, "2139.00")

	def test_free_order_books_at_zero_not_the_price_list(self):
		# Koomyo sold a complimentary item: 0.00 due, 0.00 paid. ERPNext must not re-price it.
		event = order_event(
			lines=[
				{
					"item_id": "menu-free",
					"name": "Chicken Max",
					"size": None,
					"size_id": None,
					"quantity": 1,
					"unit_price": "0.00",
					"line_total": "0.00",
				}
			],
			payments=[("cash", "0.00")],
			items_subtotal="0.00",
			total="0.00",
			amount_due="0.00",
			amount_paid="0.00",
		)
		# The Item has a list price of 499 (as every loaded menu item does).
		from koomyo_erpnext.sync.items import menu_item

		code = menu_item(self.conn_a, event["data"]["items"][0])
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": code,
				"price_list": "_Koomyo Selling",
				"price_list_rate": 499,
			}
		).insert()
		log = self.book(event)
		self.assertEqual(log.status, "Done", log.message)
		inv = self.invoice()
		self.assertMoney(inv.grand_total, "0.00")
		self.assertMoney(inv.items[0].amount, "0.00")
		self.assertEqual(inv.docstatus, 1)
		self.assertEqual(self.payments(inv), [], "nothing was paid, so no Payment Entry")
		self.assertEqual(gl("Sales Invoice", inv.name), {})

	def test_totals_that_do_not_add_up_book_nothing(self):
		event = order_event(
			lines=[{"quantity": 1, "unit_price": "1000.00", "line_total": "1000.00"}],
			payments=[("cash", "1150.01")],
			items_subtotal="1000.00",
			tax="150.00",
			total="1150.01",
			amount_due="1150.01",
			amount_paid="1150.01",
		)
		log = self.book(event)
		self.assertEqual((log.status, log.error_class), ("Failed", "Fatal"))
		self.assertIn("do not add up", log.message)
		self.assertIsNone(self.invoice())
		self.assertEqual(frappe.get_all("Item", filters={"koomyo_connection": self.conn_a.name}), [])

	def test_missing_payment_mapping_then_replay(self):
		event = order_event(
			lines=[{"quantity": 1, "unit_price": "100.00", "line_total": "100.00"}],
			payments=[("jazzcash", "115.00")],
			items_subtotal="100.00",
			tax="15.00",
			total="115.00",
			amount_due="115.00",
			amount_paid="115.00",
		)
		log = self.book(event)
		self.assertEqual((log.status, log.error_class), ("Failed", "NeedsMapping"))
		self.assertIn("'jazzcash'", log.message)
		self.assertIn("Payment Map", log.message)
		self.assertIsNone(self.invoice(), "the invoice is rolled back with the failed payment")

		self.conn_a.append(
			"payment_map",
			{"koomyo_method": "jazzcash", "mode_of_payment": "Wire Transfer", "account": self.acc["bank"]},
		)
		self.conn_a.save()
		from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry

		retry([log.name])
		events.process_pending(self.conn_a.name)
		self.assertEqual(frappe.db.get_value("Koomyo Event Log", log.name, "status"), "Done")
		self.assertMoney(self.invoice().grand_total, "115.00")

	def test_missing_branch_and_tax_mapping(self):
		event = order_event(
			lines=[{"quantity": 1, "unit_price": "100.00", "line_total": "100.00"}],
			payments=[("cash", "115.00")],
			items_subtotal="100.00",
			tax="15.00",
			total="115.00",
			amount_due="115.00",
			amount_paid="115.00",
		)
		event["data"]["branch_id"] = "unmapped-branch"
		log = self.book(event)
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn("unmapped-branch", log.message)

		self.conn_a.reload()
		self.conn_a.set("tax_map", [])
		self.conn_a.save()
		event2 = order_event(
			seq=11,
			order_id="order-2",
			lines=[{"quantity": 1, "unit_price": "100.00", "line_total": "100.00"}],
			payments=[("cash", "115.00")],
			items_subtotal="100.00",
			tax="15.00",
			total="115.00",
			amount_due="115.00",
			amount_paid="115.00",
		)
		log2 = self.book(event2)
		self.assertEqual(log2.error_class, "NeedsMapping")
		self.assertIn("Tax Map", log2.message)

	def test_account_of_other_company_refused_at_posting(self):
		frappe.db.set_value("Koomyo Tax Map", {"parent": self.conn_a.name}, "account_head", "GST - _KTB")
		self.conn_a.reload()
		log = self.book(
			order_event(
				lines=[{"quantity": 1, "unit_price": "100.00", "line_total": "100.00"}],
				payments=[("cash", "115.00")],
				items_subtotal="100.00",
				tax="15.00",
				total="115.00",
				amount_due="115.00",
				amount_paid="115.00",
			)
		)
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn("Tax Map row 1", log.message)
		self.assertIn(COMPANY_B, log.message)

	def test_replay_and_duplicate_webhook_change_nothing(self):
		event = load_fixture("event_order_paid") | {"tenant_id": TENANT_A}
		body = event_body(event)
		for _ in range(2):
			self.assertEqual(post_webhook(self.conn_a.webhook_token, body, sign(SECRET_A, body))[0], 200)
		events.process_pending(self.conn_a.name)
		# The same order again under another event id (e.g. a resend), through the feed.
		again = copy.deepcopy(event) | {"id": "another-event-id", "seq": 99}
		self.assertEqual(self.book(again).status, "Done")
		invoices = frappe.get_all("Sales Invoice", filters={"koomyo_connection": self.conn_a.name})
		self.assertEqual(len(invoices), 1)
		self.assertEqual(
			len(frappe.get_all("Payment Entry", filters={"koomyo_connection": self.conn_a.name})), 1
		)

	def test_same_order_id_in_two_connections_two_invoices(self):
		configure_sales(self.conn_b, "_KTB")
		self.conn_b.reload()
		ev_a = order_event(
			order_id="shared-id",
			tenant=TENANT_A,
			lines=[{"quantity": 1, "unit_price": "100.00", "line_total": "100.00"}],
			payments=[("cash", "115.00")],
			items_subtotal="100.00",
			tax="15.00",
			total="115.00",
			amount_due="115.00",
			amount_paid="115.00",
		)
		ev_b = copy.deepcopy(ev_a) | {"tenant_id": TENANT_B}
		self.assertEqual(self.book(ev_a).status, "Done")
		self.assertEqual(self.book(ev_b, self.conn_b, self.fake_b).status, "Done")
		a, b = self.invoice("shared-id"), self.invoice("shared-id", self.conn_b)
		self.assertNotEqual(a.name, b.name)
		self.assertEqual((a.company, b.company), (COMPANY_A, COMPANY_B))
		self.assertEqual(gl("Sales Invoice", b.name)["Sales Tax - _KTB"], D("-15.00"))
		self.assertNotEqual(
			a.items[0].item_code, b.items[0].item_code, "items are not shared between connections"
		)

	def test_unpaid_order_skipped_and_switch_off(self):
		event = order_event()
		event["data"]["payment"]["status"] = "cod_pending"
		self.assertEqual(self.book(event).status, "Skipped")
		self.conn_a.reload()
		self.conn_a.sync_sales = 0
		self.conn_a.save()
		log = self.book(order_event(seq=12, order_id="order-9"))
		self.assertEqual(log.status, "Skipped")
		self.assertIn("Sync Sales is off", log.message)

	def test_dry_run_saves_nothing_then_books(self):
		self.conn_a.reload()
		self.conn_a.dry_run = 1
		self.conn_a.save()
		log = self.book(load_fixture("event_order_paid") | {"tenant_id": TENANT_A})
		self.assertEqual((log.status, log.dry_run), ("Skipped", 1))
		self.assertIn("Sales Invoice", log.message)
		self.assertIn("724.50", log.message)
		self.assertEqual(frappe.get_all("Sales Invoice", filters={"koomyo_connection": self.conn_a.name}), [])
		self.assertEqual(frappe.get_all("Item", filters={"koomyo_connection": self.conn_a.name}), [])

		self.conn_a.reload()
		self.conn_a.dry_run = 0
		self.conn_a.save()
		self.conn_a.book_dry_run_events()
		events.process_pending(self.conn_a.name)
		self.assertEqual(
			frappe.db.get_value("Koomyo Event Log", log.name, ["status", "dry_run"]), ("Done", 0)
		)
		self.assertMoney(self.invoice("a8955e7a-335a-4ad0-a872-56b6d2479f80").grand_total, "724.50")

	def test_scopes_and_events_follow_switches(self):
		self.conn_a.reload()
		self.assertIn("order.paid", self.conn_a.events_to_register)
		self.assertIn("refund.created", self.conn_a.events_to_register)
		self.assertIn("orders.read", events.required_scopes(self.conn_a))
		self.conn_a.sync_sales = 0
		self.conn_a.sync_refunds = 0
		self.conn_a.save()
		self.assertNotIn("order.paid", self.conn_a.events_to_register)


class TestRefunds(SalesTestCase):
	def sale(self):
		self.assertEqual(self.book(load_fixture("event_order_paid") | {"tenant_id": TENANT_A}).status, "Done")
		return self.invoice("a8955e7a-335a-4ad0-a872-56b6d2479f80")

	def test_docs_example_full_refund(self):
		sale = self.sale()
		cancel = load_fixture("event_refund_created") | {"tenant_id": TENANT_A}
		log = self.book(cancel)
		self.assertEqual(log.status, "Done", log.message)
		note = frappe.get_doc("Sales Invoice", log.reference_name)
		self.assertEqual((note.is_return, note.return_against, note.docstatus), (1, sale.name, 1))
		self.assertEqual(note.koomyo_id, "f2a6054e-c2ce-4ead-a08a-ecc7a22f2871")
		self.assertMoney(note.grand_total, "-724.50")
		self.assertEqual(
			gl("Sales Invoice", note.name),
			{
				"Debtors - _KTA": D("-724.50"),
				"Sales - _KTA": D("900.00"),
				"Sales Promotions - _KTA": D("-270.00"),
				"Sales Tax - _KTA": D("94.50"),
			},
		)
		self.assertMoney(note.outstanding_amount, "0.00")
		(pe,) = self.payments(note)
		self.assertEqual(pe.payment_type, "Pay")
		self.assertEqual(
			gl("Payment Entry", pe.name), {"Cash - _KTA": D("-724.50"), "Debtors - _KTA": D("724.50")}
		)

		cancelled = load_fixture("event_order_cancelled") | {"tenant_id": TENANT_A}
		self.assertEqual(self.book(cancelled).status, "Done")
		self.assertTrue(
			frappe.db.exists(
				"Comment", {"reference_name": sale.name, "content": ("like", "%Cancelled in Koomyo%")}
			)
		)

	def test_out_of_order_refund_fetches_the_order(self):
		order = load_fixture("event_order_paid")["data"]
		self.fake_a.orders[order["id"]] = order
		log = self.book(load_fixture("event_refund_created") | {"tenant_id": TENANT_A})
		self.assertEqual(log.status, "Done", log.message)
		self.assertMoney(self.invoice(order["id"]).grand_total, "724.50")
		# The order.paid event arrives late: nothing doubles.
		self.assertEqual(
			self.book(load_fixture("event_order_paid") | {"tenant_id": TENANT_A, "seq": 30}).status, "Done"
		)
		self.assertEqual(
			len(
				frappe.get_all(
					"Sales Invoice", filters={"koomyo_connection": self.conn_a.name, "is_return": 0}
				)
			),
			1,
		)

	def test_refund_before_sale_not_in_koomyo_yet_is_retryable(self):
		log = self.book(refund_event(order_id="unknown-order"))
		self.assertEqual(
			(log.status, log.error_class), ("Failed", "Fatal")
		)  # 404 from Koomyo: not retried blindly
		self.conn_a.reload()
		self.conn_a.sync_sales = 0
		self.conn_a.save()
		log = self.book(refund_event(seq=21, refund_id="refund-2", order_id="another-order"))
		self.assertEqual((log.status, log.error_class), ("Failed", "Retryable"))

	def test_partial_refund_left_for_manual_booking_by_default(self):
		self.sale()
		log = self.book(refund_event(order_id="a8955e7a-335a-4ad0-a872-56b6d2479f80", amount="100.00"))
		self.assertEqual((log.status, log.error_class), ("Failed", "Fatal"))
		self.assertIn("Proportional Credit Note", log.message)
		self.assertEqual(
			frappe.get_all("Sales Invoice", filters={"is_return": 1, "koomyo_connection": self.conn_a.name}),
			[],
		)

	def test_partial_refunds_proportional_exact(self):
		self.conn_a.reload()
		self.conn_a.partial_refunds = "Proportional Credit Note"
		self.conn_a.save()
		sale = self.sale()
		order_id = "a8955e7a-335a-4ad0-a872-56b6d2479f80"
		# 100.00 of 724.50: tax share 94.50 * 100/724.50 = 13.04, revenue 86.96.
		log = self.book(refund_event(order_id=order_id, amount="100.00"))
		self.assertEqual(log.status, "Done", log.message)
		note = frappe.get_doc("Sales Invoice", log.reference_name)
		self.assertMoney(note.grand_total, "-100.00")
		self.assertEqual(
			gl("Sales Invoice", note.name),
			{
				"Debtors - _KTA": D("-100.00"),
				"Sales - _KTA": D("86.96"),
				"Sales Tax - _KTA": D("13.04"),
			},
		)
		# The rest in a second refund: 624.50 -> tax 81.46, revenue 543.04; together they mirror the sale.
		log2 = self.book(
			refund_event(seq=21, refund_id="refund-2", order_id=order_id, amount="624.50", method="card")
		)
		self.assertEqual(log2.status, "Done", log2.message)
		total_tax = sum(
			gl("Sales Invoice", n)["Sales Tax - _KTA"] for n in (log.reference_name, log2.reference_name)
		)
		self.assertEqual(total_tax, D("94.50"))
		# Nothing left to refund.
		log3 = self.book(refund_event(seq=22, refund_id="refund-3", order_id=order_id, amount="0.01"))
		self.assertEqual(log3.error_class, "Fatal")
		self.assertIn("more than", log3.message)
		self.assertMoney(frappe.db.get_value("Sales Invoice", sale.name, "outstanding_amount"), "0.00")

	def test_refund_replay_changes_nothing(self):
		self.sale()
		event = load_fixture("event_refund_created") | {"tenant_id": TENANT_A}
		self.book(event)
		self.book(copy.deepcopy(event) | {"id": "resent", "seq": 40})
		self.assertEqual(
			len(
				frappe.get_all(
					"Sales Invoice", filters={"is_return": 1, "koomyo_connection": self.conn_a.name}
				)
			),
			1,
		)
		self.assertEqual(
			len(
				frappe.get_all(
					"Payment Entry", filters={"payment_type": "Pay", "koomyo_connection": self.conn_a.name}
				)
			),
			1,
		)


class TestUniqueIndex(SalesTestCase):
	def test_documents_not_from_koomyo_are_unaffected(self):
		names = []
		for i in range(2):
			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": f"_KTEST-PLAIN-{i}",
					"item_group": "_Koomyo Menu",
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert(ignore_permissions=True)
			names.append(item.name)
		self.assertEqual(
			frappe.get_all(
				"Item", filters={"name": ("in", names), "koomyo_id": ("is", "not set")}, pluck="name"
			).__len__(),
			2,
		)
		for name in names:
			frappe.delete_doc("Item", name, force=True)

	def test_database_refuses_a_second_copy(self):
		event = load_fixture("event_order_paid") | {"tenant_id": TENANT_A}
		event["data"]["items"][0].update({"size": None, "size_id": None})
		self.assertEqual(self.book(event).status, "Done")
		item = frappe.get_doc("Item", self.invoice("a8955e7a-335a-4ad0-a872-56b6d2479f80").items[0].item_code)
		copy_ = frappe.copy_doc(item)
		copy_.item_code = "_KTEST-COPY"
		copy_.koomyo_connection, copy_.koomyo_id = item.koomyo_connection, item.koomyo_id
		with self.assertRaises(frappe.UniqueValidationError):
			copy_.insert(ignore_permissions=True)
		frappe.db.rollback()
