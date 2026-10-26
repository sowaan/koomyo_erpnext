"""shift.closed -> one Journal Entry for the cash over or short.

Docs: docs.koomyo.com/events/cash, /guides/erp-sales#shifts-and-cash-variance, OpenAPI schema Shift.

The drawer's takings are already booked by the invoices' Payment Entries, and refunds
paid out of the drawer by the refunds' Payment Entries. Koomyo's `expected_cash` does
not subtract those refunds (docs: "expected_cash does not subtract them, so take them
from the refund events and adjust your cash account"), so the difference left to book is

    counted_cash - (expected_cash - refunds.amount)

and it goes between the drawer's cash account and the connection's Cash Over/Short
account. Koomyo's own figures are checked first; if they do not add up nothing is booked.
"""

from decimal import Decimal

import frappe
from frappe import _

from koomyo_erpnext.exceptions import Fatal, NeedsMapping, Skipped
from koomyo_erpnext.sync.common import (
	branch_row,
	check_company,
	find_existing,
	money,
	posting_date,
	setting,
	stamp,
	to_field,
)
from koomyo_erpnext.sync.events import handles


@handles("shift.opened", switch="sync_shifts", scopes=("shifts.read",))
def on_shift_opened(event: dict, connection):
	raise Skipped(
		_("A shift opening has nothing to book: the float is a transfer into the till, not income.")
	)


@handles("shift.closed", switch="sync_shifts", scopes=("shifts.read",))
def on_shift_closed(event: dict, connection):
	return book_shift(event["data"], connection)


def book_shift(shift: dict, connection):
	if shift.get("object") != "shift" or not shift.get("id"):
		raise Fatal(_("Koomyo sent something that is not a shift."))
	existing = find_existing("Journal Entry", connection, shift["id"])
	if existing:
		return ("Journal Entry", existing)
	if shift.get("status") != "closed":
		raise Skipped(_("Shift {0} is still open.").format(shift["id"]))

	opening = money(shift.get("opening_cash"), "opening_cash")
	counted = money(shift.get("counted_cash"), "counted_cash")
	expected = money(shift.get("expected_cash"), "expected_cash")
	variance = money(shift.get("cash_variance"), "cash_variance")
	rider = money(shift.get("rider_cash_handed_in"), "rider_cash_handed_in")
	refunds = money((shift.get("refunds") or {}).get("amount"), "refunds.amount")
	methods = shift.get("payments_by_method") or []
	cash_taken = sum(
		(
			money(row.get("amount"), "payments_by_method.amount")
			for row in methods
			if row.get("settles_as_cash")
		),
		Decimal(0),
	)
	if opening + cash_taken + rider != expected or counted - expected != variance:
		raise Fatal(
			_(
				"Shift {0}: Koomyo's cash figures do not add up (opening {1} + cash taken {2} + rider cash {3} "
				"against expected {4}; counted {5} - expected {4} against variance {6}). Nothing was booked."
			).format(shift["id"], opening, cash_taken, rider, expected, counted, variance)
		)

	difference = counted - (expected - refunds)
	if not difference:
		raise Skipped(_("Shift {0} balanced: no cash over or short to book.").format(shift["id"]))

	branch = branch_row(connection, shift.get("branch_id"))
	drawer = drawer_account(connection, methods)
	over_short = setting(connection, "cash_over_short_account", _("Cash Over/Short Account"))
	check_company(connection, "Account", over_short, _("Cash Over/Short Account"))

	amount = to_field(abs(difference))
	debit, credit = (drawer, over_short) if difference > 0 else (over_short, drawer)
	entry = frappe.new_doc("Journal Entry")
	entry.update(
		{
			"voucher_type": "Cash Entry",
			"company": connection.company,
			"posting_date": posting_date(shift),
			"cheque_no": shift["id"][:140],
			"cheque_date": posting_date(shift),
			"user_remark": _(
				"Koomyo shift {0}: counted {1}, expected {2}, refunds paid from the drawer {3}. "
				"Cash {4}: {5} (Koomyo's own variance {6} includes those refunds, which are booked with them)."
			).format(
				shift["id"],
				counted,
				expected,
				refunds,
				_("over") if difference > 0 else _("short"),
				abs(difference),
				variance,
			),
		}
	)
	for account, side in ((debit, "debit_in_account_currency"), (credit, "credit_in_account_currency")):
		entry.append("accounts", {"account": account, "cost_center": branch.cost_center, side: amount})
	stamp(entry, connection, shift["id"], shift.get("branch_id"))
	entry.insert(ignore_permissions=True)
	entry.submit()
	return ("Journal Entry", entry.name)


def drawer_account(connection, methods: list[dict]) -> str:
	"""The cash account of the drawer: the one account the cash-settling methods map to."""
	accounts = set()
	for row in methods:
		if not row.get("settles_as_cash"):
			continue
		for mapped in connection.payment_map:
			if mapped.koomyo_method == row.get("method"):
				accounts.add(mapped.account)
	if len(accounts) == 1:
		account = accounts.pop()
	elif connection.drawer_account:
		account = connection.drawer_account
	else:
		raise NeedsMapping(
			_(
				"Cannot tell which account is the cash drawer ({0} cash methods mapped). Set 'Cash Drawer Account' on connection {1}, then press Retry."
			).format(len(accounts), connection.name)
		)
	check_company(connection, "Account", account, _("Cash Drawer Account"))
	return account
