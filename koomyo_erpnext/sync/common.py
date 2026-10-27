"""Helpers shared by the sync handlers: the duplicate guard, mapping lookups, money and dates.

Every lookup that depends on a map raises NeedsMapping with the exact row to add,
never a guess (CLAUDE.md: "A missing mapping is never guessed").
"""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import flt, get_datetime, getdate

from koomyo_erpnext.exceptions import Fatal, NeedsMapping
from koomyo_erpnext.utils import parse_money

CENT = Decimal("0.01")


# ---------------------------------------------------------------- money


def money(value, what: str = "amount") -> Decimal:
	"""Koomyo decimal text -> Decimal; a contract break is Fatal, not a guess."""
	try:
		return parse_money(value)
	except ValueError:
		raise Fatal(_("Koomyo sent {0} as {1!r}, which is not decimal text.").format(what, value))


def cents(value: Decimal) -> Decimal:
	return value.quantize(CENT, rounding=ROUND_HALF_UP)


def to_field(value: Decimal, precision: int = 2) -> float:
	"""The last step only: a Decimal becomes the float a Frappe Currency field stores."""
	return flt(str(value), precision)


def doc_money(value) -> Decimal:
	"""A Currency value read back from ERPNext, as an exact Decimal at 2 places."""
	return cents(Decimal(str(flt(value, 2))))


# ---------------------------------------------------------------- duplicate guard


def find_existing(doctype: str, connection, koomyo_id: str, include_cancelled: bool = True):
	"""The document already made from this Koomyo record for this connection, if any."""
	filters = {"koomyo_connection": connection.name, "koomyo_id": koomyo_id}
	if not include_cancelled:
		filters["docstatus"] = ("<", 2)
	return frappe.db.get_value(doctype, filters, "name", for_update=True)


def stamp(doc, connection, koomyo_id: str, branch_id: str | None = None):
	doc.koomyo_connection = connection.name
	doc.koomyo_id = koomyo_id
	doc.koomyo_branch = branch_id


# ---------------------------------------------------------------- maps


def branch_row(connection, branch_id: str | None):
	"""The Branch Map row for a Koomyo branch. A record with no branch uses the default cost center."""
	if not branch_id:
		if not connection.default_cost_center:
			raise NeedsMapping(
				_(
					"This record has no Koomyo branch. Set 'Cost Center for Records Without a Branch' on connection {0}."
				).format(connection.name)
			)
		return frappe._dict(cost_center=connection.default_cost_center, warehouse=None, koomyo_branch_id=None)
	for row in connection.branch_map:
		if row.koomyo_branch_id == branch_id:
			check_company(connection, "Cost Center", row.cost_center, f"Branch Map row {row.idx}")
			check_company(connection, "Warehouse", row.warehouse, f"Branch Map row {row.idx}")
			return row
	raise NeedsMapping(
		_(
			"Koomyo branch {0} is not mapped. Add a Branch Map row for it on connection {1}, then press Retry."
		).format(branch_id, connection.name)
	)


def payment_row(connection, method: str | None):
	for row in connection.payment_map:
		if row.koomyo_method == method:
			check_company(connection, "Account", row.account, f"Payment Map row {row.idx}")
			if flt(row.fee_percent):
				check_company(connection, "Account", row.fee_account, f"Payment Map row {row.idx}")
			return row
	raise NeedsMapping(
		_(
			"Koomyo payment method {0!r} is not mapped. Add a Payment Map row for it on connection {1}, then press Retry."
		).format(method, connection.name)
	)


def tax_account(connection, rate_text: str, branch_id: str | None) -> str:
	"""Most exact Tax Map row wins: branch+rate, rate, branch, then an empty row."""
	rate = money(rate_text, "tax_rate")

	def rate_matches(row):
		if not (row.tax_rate or "").strip():
			return None
		try:
			return parse_money(row.tax_rate.strip()) == rate
		except ValueError:
			return False

	ranked = []
	for row in connection.tax_map:
		rate_ok = rate_matches(row)
		branch_set = bool((row.koomyo_branch_id or "").strip())
		if rate_ok is False or (branch_set and row.koomyo_branch_id.strip() != branch_id):
			continue
		ranked.append(((2 if branch_set else 0) + (1 if rate_ok else 0), row))
	if not ranked:
		raise NeedsMapping(
			_(
				"No Tax Map row matches tax rate {0}% for branch {1}. Add one on connection {2}, then press Retry."
			).format(rate_text, branch_id or "-", connection.name)
		)
	row = max(ranked, key=lambda r: r[0])[1]
	check_company(connection, "Account", row.account_head, f"Tax Map row {row.idx}")
	return row.account_head


def setting(connection, fieldname: str, label: str) -> str:
	value = connection.get(fieldname)
	if not value:
		raise NeedsMapping(_("Set {0} on connection {1}, then press Retry.").format(label, connection.name))
	return value


def check_company(connection, doctype: str, name: str | None, where: str):
	"""Everything used for a connection must belong to its Company (checked again at posting time)."""
	if not name:
		return
	company = frappe.get_cached_value(doctype, name, "company")
	if company != connection.company:
		raise NeedsMapping(
			_("{0}: {1} {2} belongs to {3}, not to {4}. Fix the row on connection {5}.").format(
				where, _(doctype), name, company or "-", connection.company, connection.name
			)
		)


# ---------------------------------------------------------------- dates


def posting_date(record: dict):
	"""Book by Koomyo's business_date (the restaurant's own day), never the UTC date."""
	value = record.get("business_date")
	if not value:
		raise Fatal(_("Koomyo sent the record without a business_date."))
	return getdate(value)


def local_time(utc_text: str | None, timezone: str | None) -> str | None:
	"""An RFC 3339 UTC time as the restaurant's wall-clock time (HH:MM:SS)."""
	if not utc_text:
		return None
	try:
		moment = get_datetime(utc_text.replace("Z", "+00:00"))
		if moment.tzinfo is None:
			return None
		return moment.astimezone(ZoneInfo(timezone or "UTC")).strftime("%H:%M:%S")
	except Exception:
		return None


def local_date(utc_text: str | None, timezone: str | None):
	"""The restaurant's calendar day of an RFC 3339 UTC time, or None."""
	if not utc_text:
		return None
	try:
		moment = get_datetime(utc_text.replace("Z", "+00:00"))
		if moment.tzinfo is None:
			return None
		return moment.astimezone(ZoneInfo(timezone or "UTC")).date()
	except Exception:
		return None


def utc_now() -> datetime:
	from datetime import timezone

	return datetime.now(timezone.utc)
