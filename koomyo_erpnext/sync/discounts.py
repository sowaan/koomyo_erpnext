"""Discounts on sales: post each line at its list price with the discount shown separately.

Docs: docs.koomyo.com/guides/discounts and OpenAPI schemas OrderLine, Order
(list_price, list_total, discount_amount, net_price, discount_source, discounts,
amounts.items_list_total, amounts.discount_total, discounts_by_source, order_discounts).

Posting (ERPNext "Enable Discount Accounting for Selling"):
- a line is posted with price_list_rate = list_price and rate = net_price / quantity, so ERPNext
  shows the discount per unit and posts it to the line's discount account;
- the order-level discount (a voucher, a staff discount, a reward off the order) is the invoice's
  discount amount with its own discount account;
- one account per discount source comes from the connection's Discount Map.

ERPNext allows one discount account per line (and one for the order). A line that mixes sources
(a promotion and a slice reward, say) is posted with the account of its largest part, and a small
Journal Entry then moves the other parts to their own accounts, so every source ends up exactly
where Koomyo says it belongs, on an invoice that still matches the receipt.

Tax is never recalculated: the caller adds Koomyo's own tax figures.
"""

from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe import _
from frappe.utils import cint

from koomyo_erpnext.exceptions import Fatal, NeedsMapping
from koomyo_erpnext.sync.common import cents, check_company, money, stamp, to_field

SOURCES = ("promotion", "voucher", "manual", "points_reward", "slice_reward", "gift_item")
CENT = Decimal("0.01")


def discount_account(connection, source: str) -> str:
	for row in connection.discount_map:
		if row.source == source:
			check_company(connection, "Account", row.account, f"Discount Map row {row.idx}")
			return row.account
	raise NeedsMapping(
		_(
			"Koomyo discount source {0!r} has no account. Add a Discount Map row for it on connection {1}, then press Retry."
		).format(source, connection.name)
	)


def accounting_is_on() -> bool:
	return bool(cint(frappe.db.get_single_value("Selling Settings", "enable_discount_accounting")))


class DiscountBook:
	"""What each discount account should hold (Koomyo's split) against what ERPNext posts on its own."""

	def __init__(self, connection):
		self.connection = connection
		self.wanted: dict[str, Decimal] = {}
		self.posted: dict[str, Decimal] = {}
		self.notes: list[str] = []

	def used(self) -> bool:
		return bool(self.posted)

	def want(self, source: str, amount: Decimal):
		account = discount_account(self.connection, source)
		self.wanted[account] = self.wanted.get(account, Decimal(0)) + amount

	def post(self, account: str, amount: Decimal):
		self.posted[account] = self.posted.get(account, Decimal(0)) + amount

	def moves(self) -> dict[str, Decimal]:
		"""Per account: positive = debit more, negative = credit back."""
		out = {}
		for account in set(self.wanted) | set(self.posted):
			diff = self.wanted.get(account, Decimal(0)) - self.posted.get(account, Decimal(0))
			if diff:
				out[account] = diff
		return out


def split_parts(parts: list[tuple[str, Decimal]], total: Decimal, book: DiscountBook, what: str) -> str:
	"""Record each source's share of `total` and return the source whose account ERPNext posts.

	If the parts do not add up to the total, the whole total goes to the largest part's account
	(the books stay exact; only the split between sources is lost) and a note says so.
	"""
	if not parts:
		raise Fatal(_("{0} has a discount of {1} but names no source.").format(what, total))
	dominant = max(parts, key=lambda p: p[1])[0]
	if sum((amount for _s, amount in parts), Decimal(0)) != total:
		book.notes.append(
			_("{0}: the discount parts did not add up to {1}; booked under {2}.").format(
				what, total, dominant
			)
		)
		book.want(dominant, total)
	else:
		for source, amount in parts:
			book.want(source, amount)
	return dominant


def unit_rows(quantity: int, net: Decimal) -> list[tuple[int, Decimal]]:
	"""(quantity, rate) rows that add up to `net` to the cent, whatever the quantity.

	net / quantity is usually exact; when it is not (100.00 for 3), the last unit takes the odd cent.
	"""
	per_unit = (net / quantity).quantize(CENT, rounding=ROUND_HALF_UP)
	if per_unit * quantity == net:
		return [(quantity, per_unit)]
	return [(quantity - 1, per_unit), (1, net - per_unit * (quantity - 1))]


def line_rows(connection, line: dict, book: DiscountBook) -> tuple[list[dict], Decimal, Decimal]:
	"""Invoice item values for one order line, as (rows, net, list value).

	The rows carry the pricing fields only; the caller adds the item, accounts and description.
	"""
	quantity = line.get("quantity")
	if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
		raise Fatal(_("Koomyo order line {0} has quantity {1!r}.").format(line.get("id"), quantity))
	net = money(
		line.get("net_price") if line.get("net_price") is not None else line.get("line_total"), "net_price"
	)
	if line.get("line_total") is not None and money(line["line_total"], "line_total") != net:
		raise Fatal(
			_("Koomyo order line {0}: net price {1} differs from line total {2}.").format(
				line.get("id"), net, line["line_total"]
			)
		)

	if line.get("list_price") is None:
		# A gift item added before Koomyo kept the gift's price: nothing is guessed, it is posted at what it cost.
		return [{"qty": q, "rate": r, "price_list_rate": r} for q, r in unit_rows(quantity, net)], net, net

	list_price = money(line["list_price"], "list_price")
	list_total = money(line.get("list_total"), "list_total")
	if list_total != list_price * quantity:
		raise Fatal(
			_("Koomyo order line {0}: list total {1} is not {2} x {3}.").format(
				line.get("id"), list_total, list_price, quantity
			)
		)
	discount = list_total - net
	if (
		line.get("discount_amount") is not None
		and money(line["discount_amount"], "discount_amount") != discount
	):
		raise Fatal(
			_("Koomyo order line {0}: discount {1} is not list total less net price.").format(
				line.get("id"), line["discount_amount"]
			)
		)
	if discount < 0:
		raise Fatal(_("Koomyo order line {0}: the net price is above the list price.").format(line.get("id")))

	rows = []
	account = None
	if discount > 0:
		parts = [
			(d["source"], money(d["amount"], "discount amount"))
			for d in line.get("discounts") or []
			if d.get("amount") is not None
		]
		if not parts and line.get("discount_source") in SOURCES:
			parts = [(line["discount_source"], discount)]
		source = split_parts(parts, discount, book, _("Line {0}").format(line.get("name") or line.get("id")))
		account = discount_account(connection, source)
		book.post(account, discount)

	for q, rate in unit_rows(quantity, net):
		row = {"qty": q, "price_list_rate": to_field(list_price), "rate": to_field(rate)}
		if discount > 0:
			row["discount_account"] = account
			if rate == 0:
				# ERPNext fills a rate of 0 from the Item's own price list. A 100% line discount keeps
				# the list price Koomyo sent and a line total of exactly 0.
				row["discount_percentage"] = 100
			else:
				row["discount_amount"] = to_field(list_price - rate)
		rows.append(row)
	return rows, net, list_total


def order_discount(connection, order: dict, amounts: dict[str, Decimal], book: DiscountBook, invoice):
	"""The order-level discount (voucher, staff discount, reward off the order) on the invoice."""
	total = amounts["discount"] + amounts["voucher_discount"]
	if not total:
		return
	parts = [
		(d["source"], money(d["amount"], "order discount"))
		for d in order.get("order_discounts") or []
		if d.get("amount") is not None
	]
	if not parts:  # an older answer without the breakdown
		if amounts["voucher_discount"]:
			parts.append(("voucher", amounts["voucher_discount"]))
		if amounts["discount"]:
			parts.append(("manual", amounts["discount"]))
	source = split_parts(parts, total, book, _("Order {0}").format(order.get("number")))
	account = discount_account(connection, source)
	invoice.apply_discount_on = "Net Total"
	invoice.discount_amount = to_field(total)
	invoice.additional_discount_account = account
	book.post(account, total)


def reclass_entry(connection, invoice, order: dict, book: DiscountBook, cost_center: str):
	"""Move discount amounts between accounts so each source holds exactly what Koomyo says."""
	moves = book.moves()
	if not moves:
		return None
	entry = frappe.new_doc("Journal Entry")
	entry.update(
		{
			"voucher_type": "Journal Entry",
			"company": connection.company,
			"posting_date": invoice.posting_date,
			"user_remark": _(
				"Koomyo order {0}: discounts of mixed sources moved to their own accounts."
			).format(order.get("number")),
		}
	)
	for account, amount in sorted(moves.items()):
		entry.append(
			"accounts",
			{
				"account": account,
				"cost_center": cost_center,
				"debit_in_account_currency": to_field(amount) if amount > 0 else 0,
				"credit_in_account_currency": to_field(-amount) if amount < 0 else 0,
			},
		)
	stamp(entry, connection, f"discount:{order['id']}", invoice.koomyo_branch)
	entry.insert(ignore_permissions=True)
	entry.submit()
	return entry


def given_back(refund: dict) -> list[tuple[str, Decimal]]:
	"""The refund's share of the order's discounts by source (Refund.discount_given_back), as exact Decimals."""
	return [
		(part["source"], money(part["amount"], "discount given back"))
		for part in refund.get("discount_given_back") or []
		if part.get("source") and part.get("amount") is not None and money(part["amount"], "amount") != 0
	]


def refund_reclass(connection, credit_note, refund: dict, parts: list[tuple[str, Decimal]], cost_center):
	"""Make each discount account hold exactly the part of the discount this refund gives back.

	ERPNext credits the discount accounts of the credit note on its own (a row has one account); Koomyo
	says which source each part belongs to. The difference per account is moved with a small Journal Entry
	(`discount-refund:<refund id>`), so a partial refund reverses the original reclassification in part.
	If the two totals differ by rounding, the difference stays with the largest part so the entry balances.
	"""
	accounts = {row.account for row in connection.discount_map}
	posted: dict[str, Decimal] = {}
	for entry in frappe.get_all(
		"GL Entry",
		filters={
			"voucher_type": "Sales Invoice",
			"voucher_no": credit_note.name,
			"is_cancelled": 0,
			"account": ("in", list(accounts)),
		},
		fields=["account", "debit", "credit"],
	):
		posted[entry.account] = (
			posted.get(entry.account, Decimal(0)) + Decimal(str(entry.credit)) - Decimal(str(entry.debit))
		)
	wanted: dict[str, Decimal] = {}
	for source, amount in parts:
		account = discount_account(connection, source)
		wanted[account] = wanted.get(account, Decimal(0)) + amount
	residual = sum(posted.values(), Decimal(0)) - sum(wanted.values(), Decimal(0))
	if wanted and residual:
		largest = max(wanted, key=lambda a: wanted[a])
		wanted[largest] += residual
	moves = {}
	for account in set(wanted) | set(posted):
		diff = wanted.get(account, Decimal(0)) - posted.get(account, Decimal(0))
		if diff:
			moves[account] = diff  # positive: credit more (the refund gives more back here)
	if not moves:
		return None
	entry = frappe.new_doc("Journal Entry")
	entry.update(
		{
			"voucher_type": "Journal Entry",
			"company": connection.company,
			"posting_date": credit_note.posting_date,
			"user_remark": _(
				"Koomyo refund {0}: the discount it gives back, moved to its own accounts."
			).format(refund.get("order_number")),
		}
	)
	for account, diff in sorted(moves.items()):
		entry.append(
			"accounts",
			{
				"account": account,
				"cost_center": cost_center,
				"credit_in_account_currency": to_field(diff) if diff > 0 else 0,
				"debit_in_account_currency": to_field(-diff) if diff < 0 else 0,
			},
		)
	stamp(entry, connection, f"discount-refund:{refund['id']}", credit_note.koomyo_branch)
	entry.insert(ignore_permissions=True)
	entry.submit()
	return entry


def reverse_reclass(connection, order_id: str, refund_id: str, posting_date):
	"""A full refund mirrors the invoice, discount rows included; reverse the reclassification too."""
	from koomyo_erpnext.sync.common import find_existing

	name = find_existing("Journal Entry", connection, f"discount:{order_id}", include_cancelled=False)
	if not name or find_existing("Journal Entry", connection, f"discount-refund:{refund_id}"):
		return None
	original = frappe.get_doc("Journal Entry", name)
	entry = frappe.new_doc("Journal Entry")
	entry.update(
		{
			"voucher_type": "Journal Entry",
			"company": original.company,
			"posting_date": posting_date,
			"user_remark": _("Reverses the discount reclassification of {0} (refund).").format(original.name),
		}
	)
	for row in original.accounts:
		entry.append(
			"accounts",
			{
				"account": row.account,
				"cost_center": row.cost_center,
				"debit_in_account_currency": row.credit_in_account_currency,
				"credit_in_account_currency": row.debit_in_account_currency,
			},
		)
	stamp(entry, connection, f"discount-refund:{refund_id}", original.koomyo_branch)
	entry.insert(ignore_permissions=True)
	entry.submit()
	return entry


__all__ = ["DiscountBook", "cents", "line_rows", "order_discount", "reclass_entry", "reverse_reclass"]
