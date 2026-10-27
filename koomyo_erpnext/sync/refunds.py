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
from koomyo_erpnext.exceptions import Fatal, NeedsMapping, Retryable, Skipped
from koomyo_erpnext.sync import discounts
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

	mirrored = amount == invoice_total and not refunded_before
	parts = discounts.given_back(refund)
	if parts and not discounts.accounting_is_on():
		raise NeedsMapping(
			_(
				"Refund {0} gives back a discount, but Selling Settings has 'Enable Discount Accounting for Selling' off. Switch it on, then press Retry."
			).format(refund["id"])
		)
	if mirrored:
		credit_note = mirror_credit_note(original)
		check_refund_lines(original, refund)
	elif connection.partial_refunds == PROPORTIONAL:
		credit_note = proportional_credit_note(original, amount, invoice_total, connection, parts)
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
	if parts:
		# Koomyo says how much of each discount this refund gives back: move it to the right accounts.
		discounts.refund_reclass(connection, credit_note, refund, parts, credit_note.cost_center)
	elif mirrored:
		# An answer without the split (an older Koomyo): a full refund reverses the order's reclassification.
		discounts.reverse_reclass(connection, refund["order_id"], refund["id"], credit_note.posting_date)

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


def check_refund_lines(original, refund: dict):
	"""`lines` is filled only for a refund of everything collected: each line must be on the invoice, at its net price.

	The credit note mirrors the invoice, which is exactly these lines. An invoice booked before its rows
	carried the Koomyo line id cannot be checked and is taken as it is.
	"""
	lines = refund.get("lines") or []
	if not lines:
		return
	rows = [r for r in original.items if r.get("koomyo_line_id")]
	if not rows:
		return
	net_by_line: dict[str, Decimal] = {}
	for row in rows:
		net_by_line[row.koomyo_line_id] = net_by_line.get(row.koomyo_line_id, Decimal(0)) + doc_money(
			row.amount
		)
	for line in lines:
		booked = net_by_line.get(line.get("line_id"))
		if booked is None:
			raise Fatal(
				_("Refund {0} pays back line {1} ({2}), which is not on Sales Invoice {3}.").format(
					refund["id"], line.get("line_id"), line.get("name"), original.name
				)
			)
		if booked != money(line.get("amount"), "refund line amount"):
			raise Fatal(
				_("Refund {0}: line {1} is {2} in Koomyo but {3} on Sales Invoice {4}.").format(
					refund["id"], line.get("name"), line["amount"], booked, original.name
				)
			)
	if len(lines) != len(net_by_line):
		raise Fatal(
			_("Refund {0} lists {1} lines but Sales Invoice {2} has {3}.").format(
				refund["id"], len(lines), original.name, len(net_by_line)
			)
		)


def proportional_credit_note(
	original, amount: Decimal, invoice_total: Decimal, connection, parts: list | None = None
):
	"""Reverse each tax/charge row by amount/invoice_total; revenue takes the rounding so the total is exact.

	When Koomyo says how much of the order's discount the refund gives back (`parts`), the revenue row is at
	the price before that discount with the discount shown, so income and discount accounts are reversed
	by the share Koomyo names; the total is still exactly the refund.
	"""
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
	row = {
		"item_code": partial_refund_item(connection),
		"item_name": _("Partial refund of {0}").format(original.name),
		"description": _("Partial refund of {0}").format(original.name),
		"qty": -1,
		"rate": to_field(revenue),
		"income_account": first.income_account,
		"cost_center": first.cost_center,
	}
	back = sum((a for _s, a in parts or []), Decimal(0))
	if back:
		dominant = max(parts, key=lambda p: p[1])[0]
		row.update(
			{
				"price_list_rate": to_field(revenue + back),
				"discount_amount": to_field(back),
				"discount_account": discounts.discount_account(connection, dominant),
			}
		)
	note.append("items", row)
	return note
