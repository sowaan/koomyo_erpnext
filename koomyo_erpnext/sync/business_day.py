"""Fill Koomyo Business Day on what was booked before Koomyo sent it.

Koomyo gives each order, refund and shift a `business_day` (docs: conventions#the-business-day-of-a-branch). Documents
booked earlier have none, so a filter by Koomyo Business Day finds nothing for them. This reads the lists (orders,
refunds, shifts) and writes the day Koomyo gives onto what is already booked: the invoice and its payments, the credit
note and its payments, the order's stock entries, the shift's journal entry. It never works a day out here (Koomyo
answers it from the branch's day start of the moment you ask), only fills a field that is empty, never writes to
Koomyo, and is safe to run again. A preview counts and writes nothing.
"""

import frappe
from frappe import _
from frappe.utils import getdate

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import KoomyoAPIError
from koomyo_erpnext.sync.catalog import read_list
from koomyo_erpnext.sync.common import find_existing

FIELD = "koomyo_business_day"


def blank() -> dict:
	return {"filled": 0, "already": 0, "not_booked": 0, "no_day": 0}


def set_day(doctype: str, name: str, day, preview: bool) -> bool:
	"""True when the document had no day and now has it (or would have, in a preview)."""
	if frappe.db.get_value(doctype, name, FIELD):
		return False
	if not preview:
		frappe.db.set_value(doctype, name, FIELD, day, update_modified=False)
	return True


def payment_entries_of(invoice: str) -> list[str]:
	return frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": invoice},
		pluck="parent",
		distinct=True,
	)


def fill_one(connection, doctype: str, record: dict, kind: dict, preview: bool, extra: list[tuple]) -> None:
	raw = record.get("business_day")
	if not raw:
		kind["no_day"] += 1  # an older Koomyo that does not send it
		return
	try:
		day = getdate(raw)
	except Exception:
		kind["no_day"] += 1
		return
	name = find_existing(doctype, connection, record["id"])
	if not name:
		kind["not_booked"] += 1
		return
	changed = set_day(doctype, name, day, preview)
	if doctype == "Sales Invoice":
		for payment in payment_entries_of(name):
			changed = set_day("Payment Entry", payment, day, preview) or changed
	for other_doctype, koomyo_id in extra:
		other = find_existing(other_doctype, connection, koomyo_id)
		if other:
			changed = set_day(other_doctype, other, day, preview) or changed
	kind["filled" if changed else "already"] += 1


def fill_business_days(
	connection_name: str, preview: bool = False, client: KoomyoClient | None = None
) -> dict:
	"""Background job. Returns (and notes on the connection) what was found and filled."""
	connection = frappe.get_doc("Koomyo Connection", connection_name)
	client = client or KoomyoClient.for_connection(connection)
	summary = {
		"preview": bool(preview),
		"orders": blank(),
		"refunds": blank(),
		"shifts": blank(),
		"errors": [],
	}
	try:
		for order in read_list(client, "/orders", {"payment_status": "paid", "limit": 200}):
			fill_one(
				connection,
				"Sales Invoice",
				order,
				summary["orders"],
				preview,
				[("Stock Entry", f"order:{order['id']}"), ("Stock Entry", f"order-restore:{order['id']}")],
			)
		for refund in read_list(client, "/refunds", {"limit": 200}):
			fill_one(connection, "Sales Invoice", refund, summary["refunds"], preview, [])
		for shift in read_list(client, "/shifts", {"status": "closed", "limit": 200}):
			fill_one(connection, "Journal Entry", shift, summary["shifts"], preview, [])
	except KoomyoAPIError as e:
		summary["errors"].append(str(e)[:300])
	if not preview:
		frappe.db.commit()
	note(connection, summary)
	return summary


def line(kind: dict) -> str:
	return _("{0} filled, {1} already had it, {2} not booked here, {3} without a day from Koomyo").format(
		kind["filled"], kind["already"], kind["not_booked"], kind["no_day"]
	)


def note(connection, summary: dict):
	"""The result, as a comment on the connection (a background job has nobody to show it to)."""
	title = (
		_("Business day preview (nothing was written)") if summary["preview"] else _("Business day filled")
	)
	text = "<br>".join(
		[
			title,
			_("Orders: {0}").format(line(summary["orders"])),
			_("Refunds: {0}").format(line(summary["refunds"])),
			_("Shifts: {0}").format(line(summary["shifts"])),
			*[frappe.utils.escape_html(e) for e in summary["errors"]],
		]
	)
	connection.add_comment("Comment", text)
	frappe.db.commit()
