"""order.paid -> submitted Sales Invoice + one Payment Entry per payment.

Docs: docs.koomyo.com/events/sales, /guides/erp-sales, /conventions#how-an-orders-totals-add-up,
OpenAPI schemas Order, OrderLine, OrderPayment.

The invoice reproduces Koomyo's numbers exactly; nothing is recomputed:

- lines: one per non-voided order line at Koomyo's `unit_price` x `quantity`; when
  `line_total` is more than that (modifiers, combo upgrades) the difference is its own line,
  so every line adds up to Koomyo's `line_total` and the lines to `items_subtotal`.
- `discount` + `voucher_discount` -> the invoice Discount Amount (on Net Total).
- taxes and charges are "Actual" rows with Koomyo's amounts: tax (Tax Map account),
  delivery fee, channel markup, and the payment-method discount (negative) or surcharge.
- tax-inclusive prices: the tax is moved out of income with a negative row on the income
  account, so revenue = base - tax and the tax account gets exactly `tax`.
- rounding is off (disable_rounded_total), and the posted grand total must equal
  Koomyo's `amount_due` to the cent or nothing is saved.
"""

from decimal import Decimal

import frappe
from frappe import _

from koomyo_erpnext.exceptions import Fatal, Retryable, Skipped
from koomyo_erpnext.sync.common import (
	branch_row,
	check_company,
	doc_money,
	find_existing,
	local_time,
	money,
	payment_row,
	posting_date,
	setting,
	stamp,
	tax_account,
	to_field,
)
from koomyo_erpnext.sync.events import handles
from koomyo_erpnext.sync.items import menu_item

SALES_SCOPES = ("orders.read", "catalog.read")


@handles("order.paid", switch="sync_sales", scopes=SALES_SCOPES)
def on_order_paid(event: dict, connection):
	return book_order(event["data"], connection, timezone=event.get("timezone"))


@handles("order.cancelled", switch="sync_sales", scopes=SALES_SCOPES)
def on_order_cancelled(event: dict, connection):
	"""The money went back with refund.created (sent first). Here we only note it on the invoice."""
	order = event["data"]
	invoice = find_existing("Sales Invoice", connection, order["id"])
	if not invoice:
		raise Retryable(_("The Sales Invoice for order {0} is not booked yet.").format(order.get("number")))
	note = _("Cancelled in Koomyo on {0}. Reason: {1}").format(
		order.get("cancelled_at") or "-", order.get("cancel_reason") or "-"
	)
	if not frappe.db.exists(
		"Comment", {"reference_doctype": "Sales Invoice", "reference_name": invoice, "content": note}
	):
		frappe.get_doc("Sales Invoice", invoice).add_comment("Comment", note)
	return ("Sales Invoice", invoice)


def book_order(order: dict, connection, timezone: str | None = None):
	"""Create (once) the Sales Invoice and Payment Entries of a paid Koomyo order."""
	if order.get("object") != "order" or not order.get("id"):
		raise Fatal(_("Koomyo sent something that is not an order."))

	existing = find_existing("Sales Invoice", connection, order["id"])
	if existing:
		return ("Sales Invoice", existing)

	payment = order.get("payment") or {}
	if payment.get("status") != "paid":
		raise Skipped(
			_("Order {0} is not paid (payment status {1}).").format(
				order.get("number"), payment.get("status")
			)
		)

	company_currency = frappe.get_cached_value("Company", connection.company, "default_currency")
	if order.get("currency") != company_currency:
		raise Fatal(
			_(
				"Order {0} is in {1} but Company {2} works in {3}. This app does not convert currencies."
			).format(order.get("number"), order.get("currency"), connection.company, company_currency)
		)

	branch = branch_row(connection, order.get("branch_id"))
	amounts = order.get("amounts") or {}
	a = {
		key: money(amounts.get(key), f"amounts.{key}")
		for key in (
			"items_subtotal",
			"discount",
			"voucher_discount",
			"channel_markup",
			"delivery_fee",
			"tax",
			"payment_discount",
			"amount_due",
			"amount_paid",
		)
	}
	inclusive = amounts.get("prices_include_tax")
	if not isinstance(inclusive, bool):
		raise Fatal(_("Koomyo sent prices_include_tax as {0!r}.").format(inclusive))

	expected_due = (
		a["items_subtotal"]
		- a["discount"]
		- a["voucher_discount"]
		+ (Decimal(0) if inclusive else a["tax"])
		+ a["channel_markup"]
		+ a["delivery_fee"]
		- a["payment_discount"]
	)
	if expected_due != a["amount_due"]:
		raise Fatal(
			_(
				"Order {0}: Koomyo's amounts do not add up ({1} computed, {2} due). Nothing was booked."
			).format(order.get("number"), expected_due, a["amount_due"])
		)

	income_account = setting(connection, "income_account", _("Income Account"))
	check_company(connection, "Account", income_account, _("Income Account"))
	cost_center = branch.cost_center

	invoice = frappe.new_doc("Sales Invoice")
	invoice.update(
		{
			"company": connection.company,
			"customer": order_customer(connection, order),
			"posting_date": posting_date(order),
			"set_posting_time": 1,
			"currency": company_currency,
			"conversion_rate": 1,
			"selling_price_list": setting(connection, "price_list", _("Selling Price List")),
			"price_list_currency": company_currency,
			"plc_conversion_rate": 1,
			"ignore_pricing_rule": 1,
			"update_stock": 0,
			"disable_rounded_total": 1,
			"cost_center": cost_center,
			"po_no": (order.get("number") or "")[:140],
			"remarks": _("Koomyo order {0}").format(order.get("number")),
		}
	)
	invoice.due_date = invoice.posting_date
	time = local_time(order.get("paid_at") or order.get("created_at"), timezone or connection.koomyo_timezone)
	if time:
		invoice.posting_time = time
	stamp(invoice, connection, order["id"], order.get("branch_id"))

	lines_total = Decimal(0)
	for line in order.get("items") or []:
		if line.get("voided"):
			continue
		lines_total += add_order_line(invoice, connection, line, income_account, cost_center)
	if lines_total != a["items_subtotal"]:
		raise Fatal(
			_(
				"Order {0}: the lines add up to {1} but Koomyo's items subtotal is {2}. Nothing was booked."
			).format(order.get("number"), lines_total, a["items_subtotal"])
		)
	if not invoice.items:
		raise Skipped(_("Order {0} has no lines that were not voided.").format(order.get("number")))

	order_discount = a["discount"] + a["voucher_discount"]
	if order_discount:
		invoice.apply_discount_on = "Net Total"
		invoice.discount_amount = to_field(order_discount)

	def charge(account, amount: Decimal, description: str):
		if amount:
			invoice.append(
				"taxes",
				{
					"charge_type": "Actual",
					"account_head": account,
					"cost_center": cost_center,
					"description": description,
					"tax_amount": to_field(amount),
				},
			)

	if a["tax"]:
		account = tax_account(connection, amounts.get("tax_rate"), order.get("branch_id"))
		if inclusive:
			charge(income_account, -a["tax"], _("Tax included in prices"))
		charge(account, a["tax"], _("Sales tax {0}%").format(amounts.get("tax_rate")))
	if a["delivery_fee"]:
		charge(
			account_setting(connection, "delivery_fee_account", _("Delivery Fee Account")),
			a["delivery_fee"],
			_("Delivery fee"),
		)
	if a["channel_markup"]:
		charge(
			account_setting(connection, "channel_markup_account", _("Channel Markup Account")),
			a["channel_markup"],
			_("Channel markup"),
		)
	if a["payment_discount"]:
		discount_block = order.get("discount") if isinstance(order.get("discount"), dict) else {}
		label = discount_block.get("payment_label")
		charge(
			account_setting(connection, "payment_adjustment_account", _("Payment Method Discount Account")),
			-a["payment_discount"],
			label
			or (_("Payment method discount") if a["payment_discount"] > 0 else _("Payment method charge")),
		)

	invoice.insert(ignore_permissions=True)
	if doc_money(invoice.grand_total) != a["amount_due"]:
		raise Fatal(
			_("Order {0}: ERPNext computed {1} but Koomyo says {2} is due. Nothing was booked.").format(
				order.get("number"), invoice.grand_total, a["amount_due"]
			)
		)
	invoice.submit()

	book_payments(invoice, order, connection, a["amount_paid"])
	return ("Sales Invoice", invoice.name)


def add_order_line(invoice, connection, line: dict, income_account: str, cost_center: str) -> Decimal:
	quantity = line.get("quantity")
	if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
		raise Fatal(_("Koomyo order line {0} has quantity {1!r}.").format(line.get("id"), quantity))
	unit_price = money(line.get("unit_price"), "unit_price")
	line_total = money(line.get("line_total"), "line_total")
	item_code = menu_item(connection, line)
	label = line.get("name") or item_code
	if line.get("size"):
		label = f"{label} ({line['size']})"

	invoice.append(
		"items",
		{
			"item_code": item_code,
			"item_name": label[:140],
			"description": label,
			"qty": quantity,
			"rate": to_field(unit_price),
			"income_account": income_account,
			"cost_center": cost_center,
		},
	)
	extras = line_total - unit_price * quantity
	if extras < 0:
		raise Fatal(
			_("Koomyo order line {0}: line total {1} is less than price x quantity.").format(
				line.get("id"), line_total
			)
		)
	if extras:
		names = [m.get("name") for m in line.get("modifiers") or []] + [
			c.get("name") for c in line.get("combo_selections") or []
		]
		description = _("Extras for {0}: {1}").format(label, ", ".join(n for n in names if n) or "-")
		invoice.append(
			"items",
			{
				"item_code": item_code,
				"item_name": description[:140],
				"description": description,
				"qty": 1,
				"rate": to_field(extras),
				"income_account": income_account,
				"cost_center": cost_center,
			},
		)
	return line_total


def order_customer(connection, order: dict) -> str:
	"""A customer synced from Koomyo for this connection, else the walk-in customer."""
	customer = order.get("customer") or {}
	if isinstance(customer, dict) and customer.get("id"):
		existing = find_existing("Customer", connection, customer["id"])
		if existing:
			return existing
	return setting(connection, "walk_in_customer", _("Walk-in Customer"))


def account_setting(connection, fieldname: str, label: str) -> str:
	account = setting(connection, fieldname, label)
	check_company(connection, "Account", account, label)
	return account


def book_payments(invoice, order: dict, connection, amount_paid: Decimal):
	payments = (order.get("payment") or {}).get("payments") or []
	total = sum((money(p.get("amount"), "payment amount") for p in payments), Decimal(0))
	if total != amount_paid:
		raise Fatal(
			_("Order {0}: the payments add up to {1} but Koomyo's amount paid is {2}.").format(
				order.get("number"), total, amount_paid
			)
		)
	if total > doc_money(invoice.grand_total):
		raise Fatal(_("Order {0}: more was paid than the invoice total.").format(order.get("number")))
	for p in payments:
		amount = money(p.get("amount"), "payment amount")
		if amount <= 0:
			continue
		if not p.get("id"):
			raise Fatal(_("Order {0} has a payment without an id.").format(order.get("number")))
		if find_existing("Payment Entry", connection, p["id"]):
			continue
		make_payment_entry(
			invoice, connection, p["id"], p.get("method"), amount, p.get("reference") or order.get("number")
		)


def make_payment_entry(
	invoice, connection, koomyo_id: str, method: str | None, amount: Decimal, reference: str | None
):
	"""One Payment Entry against `invoice` (a sale, or a credit note when amount is paid out)."""
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	row = payment_row(connection, method)
	is_refund = bool(invoice.is_return)
	pe = get_payment_entry(
		"Sales Invoice",
		invoice.name,
		party_amount=None if is_refund else to_field(amount),
		bank_account=row.account,
		bank_amount=None if is_refund else to_field(amount),
		ignore_permissions=True,
	)
	pe.posting_date = invoice.posting_date
	pe.reference_date = invoice.posting_date
	pe.reference_no = (reference or invoice.name)[:140]
	pe.mode_of_payment = row.mode_of_payment
	pe.cost_center = invoice.cost_center
	stamp(pe, connection, koomyo_id, invoice.koomyo_branch)

	fee_percent = Decimal(str(row.fee_percent or 0))
	if fee_percent and not is_refund:
		from koomyo_erpnext.sync.common import cents

		fee = cents(amount * fee_percent / 100)
		if fee:
			pe.append(
				"deductions",
				{
					"account": row.fee_account,
					"cost_center": invoice.cost_center,
					"amount": to_field(fee),
					"description": _("{0} fee {1}%").format(row.mode_of_payment, row.fee_percent),
				},
			)
			# ERPNext (same currency): the bank gets paid_amount, the customer is credited with
			# paid_amount + deductions, which must equal the amount allocated to the invoice.
			pe.paid_amount = pe.received_amount = to_field(amount - fee)
			pe.base_paid_amount = pe.base_received_amount = pe.paid_amount
	pe.insert(ignore_permissions=True)
	pe.submit()
	return pe
