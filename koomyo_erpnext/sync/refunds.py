"""refund.created -> a submitted Credit Note against the order's Sales Invoice, and the payment out.

Docs: docs.koomyo.com/events/sales#refundcreated, /guides/erp-sales, OpenAPI schema Refund.

A Koomyo refund carries an amount, not lines (schema Refund). So:
- a refund of the whole invoice mirrors it exactly (ERPNext's own make_sales_return);
- a partial refund follows the connection's "Partial Refunds" setting: left for manual
  booking (the safe default) or a proportional Credit Note that reverses revenue and each
  tax/charge row in the same share, with rounding on the revenue line so the total is exact.
- a refund that arrives before its sale fetches the order (GET /orders/{id}) and books it
  first, as the docs advise; if the sale cannot be booked yet the event is Retryable.
"""

from decimal import Decimal
from urllib.parse import quote

import frappe
from frappe import _

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import Fatal, Retryable, Skipped
from koomyo_erpnext.sync.common import (
	cents,
	doc_money,
	find_existing,
	local_time,
	money,
	posting_date,
	stamp,
	to_field,
)
from koomyo_erpnext.sync.events import handles
from koomyo_erpnext.sync.items import partial_refund_item
from koomyo_erpnext.sync.sales import book_order, make_payment_entry

PROPORTIONAL = "Proportional Credit Note"


@handles("refund.created", switch="sync_refunds", scopes=("orders.read",))
def on_refund_created(event: dict, connection):
	return book_refund(event["data"], connection, timezone=event.get("timezone"))


def book_refund(refund: dict, connection, timezone: str | None = None, client: KoomyoClient | None = None):
	if refund.get("object") != "refund" or not refund.get("id") or not refund.get("order_id"):
		raise Fatal(_("Koomyo sent something that is not a refund."))

	existing = find_existing("Sales Invoice", connection, refund["id"])
	if existing:
		return ("Sales Invoice", existing)

	amount = money(refund.get("amount"), "refund amount")
	if amount <= 0:
		raise Skipped(_("Refund {0} is for {1}; nothing to book.").format(refund["id"], amount))

	original_name = find_existing("Sales Invoice", connection, refund["order_id"], include_cancelled=False)
	if not original_name:
		original_name = book_missing_sale(refund, connection, timezone, client)
	original = frappe.get_doc("Sales Invoice", original_name)
	if original.docstatus != 1:
		raise Retryable(
			_("Sales Invoice {0} for order {1} is not submitted yet.").format(
				original.name, refund.get("order_number")
			)
		)

	invoice_total = doc_money(original.grand_total)
	refunded_before = -sum(
		(
			doc_money(gt)
			for gt in frappe.get_all(
				"Sales Invoice",
				filters={"return_against": original.name, "is_return": 1, "docstatus": 1},
				pluck="grand_total",
			)
		),
		Decimal(0),
	)
	left = invoice_total - refunded_before
	if amount > left:
		raise Fatal(
			_("Refund of {0} is more than the {1} left on Sales Invoice {2}.").format(
				amount, left, original.name
			)
		)

	if amount == invoice_total and not refunded_before:
		credit_note = mirror_credit_note(original)
	elif connection.partial_refunds == PROPORTIONAL:
		credit_note = proportional_credit_note(original, amount, invoice_total, connection)
	else:
		raise Fatal(
			_(
				"Refund of {0} is part of Sales Invoice {1} ({2}). Koomyo does not say which lines it covers, so it was "
				"left for you: book it by hand, or set Partial Refunds to Proportional Credit Note and press Retry."
			).format(amount, original.name, invoice_total)
		)

	credit_note.posting_date = posting_date(refund)
	credit_note.set_posting_time = 1
	time = local_time(refund.get("created_at"), timezone or connection.koomyo_timezone)
	if time:
		credit_note.posting_time = time
	credit_note.due_date = credit_note.posting_date
	credit_note.disable_rounded_total = 1
	credit_note.update_outstanding_for_self = 1
	credit_note.remarks = _("Koomyo refund for order {0}. Reason: {1}").format(
		refund.get("order_number"), refund.get("reason") or "-"
	)
	stamp(credit_note, connection, refund["id"], refund.get("branch_id"))
	credit_note.insert(ignore_permissions=True)
	if doc_money(credit_note.grand_total) != -amount:
		raise Fatal(
			_("Refund {0}: ERPNext computed {1} but Koomyo refunded {2}. Nothing was booked.").format(
				refund["id"], credit_note.grand_total, amount
			)
		)
	credit_note.submit()

	if refund.get("payment_method"):
		make_payment_entry(
			credit_note,
			connection,
			refund["id"],
			refund["payment_method"],
			amount,
			refund.get("order_number"),
		)
	return ("Sales Invoice", credit_note.name)


def book_missing_sale(refund: dict, connection, timezone, client) -> str:
	"""The refund came before its sale: fetch the order and book it first (docs: webhooks/reliable#ordering)."""
	if not connection.sync_sales:
		raise Retryable(
			_("The Sales Invoice for order {0} is not in ERPNext and Sync Sales is off.").format(
				refund.get("order_number")
			)
		)
	client = client or KoomyoClient.for_connection(connection)
	order = client.get("/orders/" + quote(refund["order_id"], safe=""))
	if order.get("id") != refund["order_id"]:
		raise Fatal(_("Koomyo answered GET /orders/{0} with another order.").format(refund["order_id"]))
	if (order.get("payment") or {}).get("status") != "paid":
		raise Retryable(_("Order {0} is not paid yet in Koomyo.").format(refund.get("order_number")))
	_doctype, name = book_order(order, connection, timezone=timezone)
	return name


def mirror_credit_note(original):
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

	return make_sales_return(original.name)


def proportional_credit_note(original, amount: Decimal, invoice_total: Decimal, connection):
	"""Reverse each tax/charge row by amount/invoice_total; revenue takes the rounding so the total is exact."""
	note = frappe.new_doc("Sales Invoice")
	note.update(
		{
			"is_return": 1,
			"return_against": original.name,
			"company": original.company,
			"customer": original.customer,
			"currency": original.currency,
			"conversion_rate": 1,
			"selling_price_list": original.selling_price_list,
			"price_list_currency": original.price_list_currency,
			"plc_conversion_rate": 1,
			"ignore_pricing_rule": 1,
			"update_stock": 0,
			"cost_center": original.cost_center,
		}
	)
	reversed_charges = Decimal(0)
	for row in original.taxes:
		share = cents(doc_money(row.tax_amount) * amount / invoice_total)
		if not share:
			continue
		reversed_charges += share
		note.append(
			"taxes",
			{
				"charge_type": "Actual",
				"account_head": row.account_head,
				"cost_center": row.cost_center,
				"description": row.description,
				"tax_amount": to_field(-share),
			},
		)
	revenue = amount - reversed_charges
	first = original.items[0]
	note.append(
		"items",
		{
			"item_code": partial_refund_item(connection),
			"item_name": _("Partial refund of {0}").format(original.name),
			"description": _("Partial refund of {0}").format(original.name),
			"qty": -1,
			"rate": to_field(revenue),
			"income_account": first.income_account,
			"cost_center": first.cost_center,
		},
	)
	return note
