# Copyright (c) 2026, Sowaan and contributors
# For license information, please see license.txt

"""Sales made from Koomyo orders, by payment method and tax rate.

One row per payment method, tax rate and kind (sale or refund). Everything is read from the booked documents:

- the payment method is the Mode of Payment of the Payment Entries that settled the invoice (a refund: the ones that
  paid it back); a sales channel order (Foodpanda, Careem) is its own line, "Channel: <code>", since the partner pays
  later; an invoice nobody has paid yet is "Not paid";
- a sale paid with two methods is divided by what each paid, every amount to the cent, with any remainder going to the
  largest part, so the rows always add up to the invoices;
- the tax rate is the one Koomyo applied to the order (Koomyo Tax Rate on the invoice; a credit note has its sale's);
- Net sales + Tax + Other charges = Total on every row. Other charges are delivery, channel markup, POS fee, packaging
  and payment-method adjustments. Tax is what went to the Tax Map accounts.

Invoices are read with the user's permissions, so a user limited to one Company sees only that Company.
"""

from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate

CENT = Decimal("0.01")
NOT_PAID = "Not paid"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not (filters.from_date and filters.to_date):
		frappe.throw(_("Choose the dates."))
	return columns(filters), data(filters)


def columns(filters=None):
	day = (
		[{"label": _("Day"), "fieldname": "day", "fieldtype": "Data", "width": 100}]
		if filters and filters.by_day
		else []
	)
	return [
		*day,
		{"label": _("Payment Method"), "fieldname": "method", "fieldtype": "Data", "width": 180},
		{"label": _("Tax Rate"), "fieldname": "rate", "fieldtype": "Data", "width": 90},
		{"label": _("Type"), "fieldname": "kind", "fieldtype": "Data", "width": 90},
		{
			"label": _("Orders (at their largest payment)"),
			"fieldname": "orders",
			"fieldtype": "Int",
			"width": 130,
		},
		{"label": _("Net Sales"), "fieldname": "net", "fieldtype": "Currency", "width": 130},
		{"label": _("Tax"), "fieldname": "tax", "fieldtype": "Currency", "width": 120},
		{"label": _("Other Charges"), "fieldname": "other", "fieldtype": "Currency", "width": 120},
		{"label": _("Total"), "fieldname": "total", "fieldtype": "Currency", "width": 130},
	]


def money(value) -> Decimal:
	return Decimal(str(flt(value, 2))).quantize(CENT)


def rate_label(text) -> str:
	"""'15.00' and '15' are the same rate; nothing sent is '-'."""
	if text in (None, ""):
		return "-"
	try:
		return f"{Decimal(str(text)).normalize():f}%"
	except Exception:
		return str(text)[:20]


def divide(amount: Decimal, shares: list[Decimal]) -> list[Decimal]:
	"""`amount` split by `shares`, each part to the cent, the remainder to the largest share: the parts add up exactly."""
	total = sum(shares, Decimal(0))
	if len(shares) == 1 or not total:
		return [amount]
	parts = [(amount * s / total).quantize(CENT, rounding=ROUND_HALF_UP) for s in shares]
	biggest = max(range(len(shares)), key=lambda i: shares[i])
	parts[biggest] += amount - sum(parts, Decimal(0))
	return parts


def data(filters) -> list[dict]:
	by_business_day = filters.basis == "Business day"
	invoice_filters = {"koomyo_connection": ("is", "set"), "docstatus": 1}
	if by_business_day:
		# The business day can differ from the posting date by a day either way: read a little wider, then keep
		# the invoices whose own day is in range (an older invoice with no business day counts on its posting date).
		invoice_filters["posting_date"] = (
			"between",
			[add_days(filters.from_date, -2), add_days(filters.to_date, 2)],
		)
	else:
		invoice_filters["posting_date"] = ("between", [filters.from_date, filters.to_date])
	if filters.company:
		invoice_filters["company"] = filters.company
	if filters.connection:
		invoice_filters["koomyo_connection"] = filters.connection
	if filters.branch:
		invoice_filters["koomyo_branch"] = filters.branch
	if filters.refunds == "Leave out":
		invoice_filters["is_return"] = 0
	invoices = frappe.get_list(
		"Sales Invoice",
		filters=invoice_filters,
		fields=[
			"name",
			"posting_date",
			"koomyo_business_day",
			"is_return",
			"koomyo_channel",
			"koomyo_tax_rate",
			"return_against",
			"net_total",
			"grand_total",
			"total_taxes_and_charges",
		],
		limit_page_length=0,
		order_by="posting_date asc, name asc",
	)
	if by_business_day:
		first, last = getdate(filters.from_date), getdate(filters.to_date)
		invoices = [i for i in invoices if first <= getdate(i.koomyo_business_day or i.posting_date) <= last]
	if not invoices:
		return []
	names = [i.name for i in invoices]
	tax_accounts = set(frappe.get_all("Koomyo Tax Map", pluck="account_head", distinct=True))
	income_accounts = {
		a for a in frappe.get_all("Koomyo Connection", pluck="income_account", distinct=True) if a
	}
	taxes = {name: {"tax": Decimal(0), "income": Decimal(0), "other": Decimal(0)} for name in names}
	for row in frappe.get_all(
		"Sales Taxes and Charges",
		filters={"parent": ("in", names), "parenttype": "Sales Invoice"},
		fields=["parent", "account_head", "tax_amount"],
	):
		kind = (
			"tax"
			if row.account_head in tax_accounts
			else "income"
			if row.account_head in income_accounts
			else "other"
		)
		taxes[row.parent][kind] += money(row.tax_amount)
	payments = payment_shares(names)
	rates = {i.name: i.koomyo_tax_rate for i in invoices}
	rows: dict[tuple, dict] = {}
	for inv in invoices:
		sign_kind = _("Refund") if inv.is_return else _("Sale")
		if filters.refunds == "Net with sales":
			sign_kind = _("Net")
		rate = rate_label(inv.koomyo_tax_rate or rates.get(inv.return_against))
		day = str(
			getdate(inv.koomyo_business_day or inv.posting_date) if by_business_day else inv.posting_date
		)
		total = money(inv.grand_total)
		tax = taxes[inv.name]["tax"]
		other = taxes[inv.name]["other"]
		net = total - tax - other  # the income reclassification inside the prices counts as sales
		methods = shares_of(inv, payments, total)
		weights = [w for _m, w in methods]
		parts = {
			key: divide(value, weights)
			for key, value in (("net", net), ("tax", tax), ("other", other), ("total", total))
		}
		main = max(range(len(methods)), key=lambda i: methods[i][1])
		for index, (method, _weight) in enumerate(methods):
			row = rows.setdefault(
				(day if filters.by_day else "", method, rate, sign_kind),
				{
					"day": day if filters.by_day else None,
					"method": method,
					"rate": rate,
					"kind": sign_kind,
					"orders": 0,
					"net": 0,
					"tax": 0,
					"other": 0,
					"total": 0,
				},
			)
			if index == main:
				row["orders"] += 1
			for key in ("net", "tax", "other", "total"):
				row[key] = money(row[key] + parts[key][index])
	return [
		{**row, **{k: flt(row[k], 2) for k in ("net", "tax", "other", "total")}}
		for _key, row in sorted(rows.items(), key=lambda item: item[0])
	]


def payment_shares(names: list[str]) -> dict[str, dict[str, Decimal]]:
	"""{invoice: {Mode of Payment: amount}} from the Payment Entries that settled (or, for a credit note, paid back)."""
	references = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": ("in", names)},
		fields=["parent", "reference_name", "allocated_amount"],
	)
	if not references:
		return {}
	modes = {
		pe.name: pe.mode_of_payment or _("No mode of payment")
		for pe in frappe.get_all(
			"Payment Entry",
			filters={"name": ("in", list({r.parent for r in references})), "docstatus": 1},
			fields=["name", "mode_of_payment"],
		)
	}
	shares: dict[str, dict[str, Decimal]] = {}
	for ref in references:
		if ref.parent not in modes:
			continue
		by_mode = shares.setdefault(ref.reference_name, {})
		by_mode[modes[ref.parent]] = by_mode.get(modes[ref.parent], Decimal(0)) + abs(
			money(ref.allocated_amount)
		)
	return shares


def shares_of(inv, payments: dict, total: Decimal) -> list[tuple[str, Decimal]]:
	"""(payment method, weight) of one invoice. A channel order is one line; the rest by what each method paid."""
	if inv.koomyo_channel:
		return [(_("Channel: {0}").format(inv.koomyo_channel), Decimal(1))]
	paid = payments.get(inv.name) or {}
	shares = [(method, amount) for method, amount in sorted(paid.items()) if amount > 0]
	covered = sum((a for _m, a in shares), Decimal(0))
	if abs(total) - covered > CENT:  # paid in part, or not yet: the rest is not assigned to a method
		shares.append((_(NOT_PAID), abs(total) - covered))
	return shares or [(_(NOT_PAID), Decimal(1))]
