"""Cash on delivery: a rider who collected short (`cod_short`) or kept extra cash (`cod_extra`).

Docs: OpenAPI Order.cod_short and Order.cod_extra (conventions "Cash on delivery collected short" and
"Extra cash kept by the rider"). Both are null on almost every order, and null is "nothing special".

- cod_short: the order's payment is what was **collected**, so the Payment Entry already matches the cash
  that arrived and the invoice is left with the shortfall unpaid. Koomyo only records the shortfall (it is not
  a refund and not written off), so the accounting is ours: when **COD Shortfall Account** is set, a Journal
  Entry clears the unpaid shortfall against the invoice to that account (a write-off). Without it the sale is
  still booked, the invoice stays partly unpaid, and the Event Log row says to set the account.
- cod_extra: the customer handed over more than the due and the rider kept it. The payment is exactly the
  amount due, so the extra is in no payment, total, tax or refund limit. A Journal Entry debits the cash
  account of the order's cash payment (the rider holds it, so rider settlement includes it) and credits
  **Rider Extra Cash Account** (income, not a sale). Without the setting it is noted on the invoice and the row
  warns.

Both entries carry `cod-short:<order id>` / `cod-extra:<order id>`, so a replay changes nothing.
"""

import frappe
from frappe import _

from koomyo_erpnext.exceptions import Fatal
from koomyo_erpnext.sync.common import check_company, find_existing, money, payment_row, stamp, to_field
from koomyo_erpnext.sync.events import warn

SHORT_REASONS = ("customer_short", "no_change", "customer_refused", "damaged_order", "other")
EXTRA_REASONS = ("no_change", "tip")


def block(order: dict, name: str):
	value = order.get(name)
	if value is None:
		return None
	if not isinstance(value, dict):
		raise Fatal(
			_("Order {0}: Koomyo sent {1} as {2!r}, not an object or null.").format(
				order.get("number"), name, value
			)
		)
	return value


def book_cod(invoice, order: dict, connection, amounts: dict):
	"""Called once the invoice and its payments are booked; `amounts` holds amount_due and amount_paid."""
	short, extra = block(order, "cod_short"), block(order, "cod_extra")
	if short:
		book_short(invoice, order, connection, short, amounts)
	if extra:
		book_extra(invoice, order, connection, extra)


def book_short(invoice, order: dict, connection, short: dict, amounts: dict):
	expected = money(short.get("expected"), "cod_short.expected")
	collected = money(short.get("collected"), "cod_short.collected")
	shortfall = money(short.get("shortfall"), "cod_short.shortfall")
	if shortfall <= 0 or collected + shortfall != expected:
		raise Fatal(
			_(
				"Order {0}: Koomyo's collected-short figures do not add up (expected {1}, collected {2}, shortfall {3}). Nothing was booked."
			).format(order.get("number"), expected, collected, shortfall)
		)
	if expected != amounts["amount_due"] or collected != amounts["amount_paid"]:
		raise Fatal(
			_(
				"Order {0}: cod_short says {1} was due and {2} collected, but the order's amounts say {3} due and {4} paid. Nothing was booked."
			).format(order.get("number"), expected, collected, amounts["amount_due"], amounts["amount_paid"])
		)
	reason = short.get("reason") if short.get("reason") in SHORT_REASONS else "other"
	note = _("Collected short on delivery: {0} of {1} ({2}).").format(
		collected, expected, reason.replace("_", " ")
	)
	frappe.get_doc("Sales Invoice", invoice.name).add_comment("Comment", note)
	account = connection.get("cod_shortfall_account")
	if not account:
		warn(
			_(
				"The rider collected {0} less than the {1} due. The invoice stays partly unpaid: set COD Shortfall Account on connection {2} to write such shortfalls off."
			).format(shortfall, expected, connection.name)
		)
		return
	check_company(connection, "Account", account, _("COD Shortfall Account"))
	koomyo_id = f"cod-short:{order['id']}"
	if find_existing("Journal Entry", connection, koomyo_id):
		return
	entry = frappe.new_doc("Journal Entry")
	entry.update(
		{
			"voucher_type": "Journal Entry",
			"company": connection.company,
			"posting_date": invoice.posting_date,
			"user_remark": _("Koomyo order {0}: {1} Written off.").format(order.get("number"), note),
		}
	)
	entry.append(
		"accounts",
		{
			"account": account,
			"cost_center": invoice.cost_center,
			"debit_in_account_currency": to_field(shortfall),
		},
	)
	entry.append(
		"accounts",
		{
			"account": invoice.debit_to,
			"party_type": "Customer",
			"party": invoice.customer,
			"reference_type": "Sales Invoice",
			"reference_name": invoice.name,
			"cost_center": invoice.cost_center,
			"credit_in_account_currency": to_field(shortfall),
		},
	)
	stamp(entry, connection, koomyo_id, invoice.koomyo_branch)
	entry.insert(ignore_permissions=True)
	entry.submit()


def cash_method(order: dict) -> str | None:
	"""The Koomyo payment method the rider's cash came in as: `cash`, else the first payment's."""
	payments = (order.get("payment") or {}).get("payments") or []
	methods = [p.get("method") for p in payments if p.get("method")]
	return "cash" if "cash" in methods else (methods[0] if methods else None)


def book_extra(invoice, order: dict, connection, extra: dict):
	amount = money(extra.get("amount"), "cod_extra.amount")
	if amount <= 0:
		raise Fatal(
			_("Order {0}: cod_extra.amount must be more than 0, not {1}.").format(order.get("number"), amount)
		)
	reason = extra.get("reason") if extra.get("reason") in EXTRA_REASONS else "other"
	note = _("Extra cash kept by the rider on delivery: {0} ({1}). Not part of the sale.").format(
		amount, reason.replace("_", " ")
	)
	frappe.get_doc("Sales Invoice", invoice.name).add_comment("Comment", note)
	income = connection.get("cod_extra_account")
	method = cash_method(order)
	if not income or not method:
		warn(
			_(
				"The rider kept {0} extra cash. It is only noted on the invoice: set Rider Extra Cash Account on connection {1} to book it."
			).format(amount, connection.name)
		)
		return
	check_company(connection, "Account", income, _("Rider Extra Cash Account"))
	cash_account = payment_row(connection, method).account
	koomyo_id = f"cod-extra:{order['id']}"
	if find_existing("Journal Entry", connection, koomyo_id):
		return
	entry = frappe.new_doc("Journal Entry")
	entry.update(
		{
			"voucher_type": "Journal Entry",
			"company": connection.company,
			"posting_date": invoice.posting_date,
			"user_remark": _("Koomyo order {0}: {1}").format(order.get("number"), note),
		}
	)
	entry.append(
		"accounts",
		{
			"account": cash_account,
			"cost_center": invoice.cost_center,
			"debit_in_account_currency": to_field(amount),
		},
	)
	entry.append(
		"accounts",
		{
			"account": income,
			"cost_center": invoice.cost_center,
			"credit_in_account_currency": to_field(amount),
		},
	)
	stamp(entry, connection, koomyo_id, invoice.koomyo_branch)
	entry.insert(ignore_permissions=True)
	entry.submit()
