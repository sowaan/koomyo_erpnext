"""Per-order stock deduction: Koomyo's own ingredient deductions, one Stock Entry per order.

Docs: docs.koomyo.com/events/stock, OpenAPI GET /stock-movements, schema StockMovement
("reason: a system code such as order_deduction, order_cancel_restore ...; reference_id:
the order, purchase order or other record behind the movement").

The connection's Stock Deduction setting picks one source for what sales take out of stock:

- Daily: stock.usage_daily, one Material Issue per branch per day (the default).
- Per Order: after each order.paid, the order's order_deduction movements become one
  Material Issue; after order.cancelled, its order_cancel_restore movements become a
  Material Receipt. Daily usage is then skipped.

Exactly one source applies to any business day, so nothing is counted twice. A change of
setting takes effect from the next day (`stock_deduction_since`); days before keep the old
setting. The stock step is its own Event Log row: if ERPNext refuses the stock (not enough
on hand), the invoice is still booked and only the stock step waits for Retry.
"""

from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import add_days, flt, get_datetime, getdate

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import Fatal, Skipped
from koomyo_erpnext.sync.common import find_existing, money, posting_date
from koomyo_erpnext.sync.events import derives, handles
from koomyo_erpnext.sync.stock import (
	STOCK_SCOPES,
	account_setting,
	add_line,
	check_opening_date,
	location_warehouse,
	new_stock_entry,
	submit_entry,
)

DAILY = "Daily (from Koomyo usage)"
PER_ORDER = "Per Order (from Koomyo movements)"
ISSUE_STEP = "koomyo.order_stock"
RESTORE_STEP = "koomyo.order_stock_restore"
# Koomyo deducts when lines go to the kitchen, which is before payment; the window around
# the order's own times is wide enough for dine-in rounds and late payment.
WINDOW_BEFORE = timedelta(minutes=5)
WINDOW_AFTER = timedelta(hours=6)


def effective_mode(connection, day) -> str:
	"""The deduction source for one business day: the setting from its start day on; before that,
	the setting it replaced (or the same one, when it was never changed)."""
	mode = connection.get("stock_deduction") or DAILY
	since = connection.get("stock_deduction_since")
	if not since or getdate(day) >= getdate(since):
		return mode
	return connection.get("stock_deduction_before") or mode


def per_order_on(connection, record: dict) -> bool:
	return bool(connection.sync_stock) and effective_mode(connection, posting_date(record)) == PER_ORDER


def next_business_day(connection):
	"""Tomorrow in the restaurant's time zone: a change of setting never splits a day."""
	from zoneinfo import ZoneInfo

	today = datetime.now(ZoneInfo(connection.koomyo_timezone or "UTC")).date()
	return add_days(today, 1)


# ------------------------------------------------------------------ queue the step


@derives("order.paid")
def after_order_paid(connection, event: dict):
	data = event.get("data") or {}
	if data.get("business_date") and per_order_on(connection, data):
		return ISSUE_STEP
	return None


@derives("order.cancelled")
def after_order_cancelled(connection, event: dict):
	data = event.get("data") or {}
	if data.get("business_date") and per_order_on(connection, data):
		return RESTORE_STEP
	return None


# ------------------------------------------------------------------ the steps


@handles(ISSUE_STEP, switch="sync_stock", scopes=STOCK_SCOPES)
def deduct_order_stock(event: dict, connection, client: KoomyoClient | None = None):
	order = event["data"]
	return book(connection, order, client, restore=False)


@handles(RESTORE_STEP, switch="sync_stock", scopes=STOCK_SCOPES)
def restore_order_stock(event: dict, connection, client: KoomyoClient | None = None):
	order = event["data"]
	return book(connection, order, client, restore=True)


def book(connection, order: dict, client, restore: bool):
	if order.get("object") != "order" or not order.get("id"):
		raise Fatal(_("Koomyo sent something that is not an order."))
	koomyo_id = f"{'order-restore' if restore else 'order'}:{order['id']}"
	existing = find_existing("Stock Entry", connection, koomyo_id)
	if existing:
		return ("Stock Entry", existing)
	if not per_order_on(connection, order):
		raise Skipped(
			_("Per-order stock deduction is not on for {0}: the daily usage books that day.").format(
				order.get("business_date")
			)
		)
	check_opening_date(connection, order)

	reason = "order_cancel_restore" if restore else "order_deduction"
	movements = order_movements(connection, client or KoomyoClient.for_connection(connection), order, reason)
	if not movements:
		raise Skipped(
			_("Koomyo recorded no ingredient {0} for order {1} (its items may have no recipe).").format(
				_("put back") if restore else _("deductions"), order.get("number")
			)
		)

	cogs = account_setting(connection, "cogs_account", _("Cost of Goods Sold Account"))
	totals: dict[tuple, dict] = {}
	for movement in movements:
		warehouse, branch = location_warehouse(connection, movement.get("location"), order.get("branch_id"))
		key = (movement["ingredient_id"], warehouse)
		line = totals.setdefault(key, {"line": movement, "warehouse": warehouse, "branch": branch, "qty": 0})
		line["qty"] += money(movement.get("quantity_change"), "quantity_change")

	entry = new_stock_entry(
		connection,
		"Material Receipt" if restore else "Material Issue",
		order,
		koomyo_id,
		order.get("branch_id"),
	)
	entry.remarks = _("Koomyo order {0}: ingredients {1}.").format(
		order.get("number"), _("put back after cancelling") if restore else _("used")
	)
	for line in totals.values():
		qty = line["qty"] if restore else -line["qty"]  # deductions are negative changes
		if qty <= 0:
			continue
		if restore:
			add_line(
				entry,
				connection,
				line["line"],
				flt(str(qty), 6),
				target=line["warehouse"],
				rate_text=line["line"].get("unit_cost"),
				expense=cogs,
				branch=line["branch"],
			)
		else:
			add_line(
				entry,
				connection,
				line["line"],
				flt(str(qty), 6),
				source=line["warehouse"],
				expense=cogs,
				branch=line["branch"],
			)
	return submit_entry(entry, _("The order's ingredient movements net to nothing."))


def order_movements(connection, client, order: dict, reason: str) -> list[dict]:
	"""This order's movements of one reason. The list cannot filter by order, so a time window
	around the order is read and the rows are matched on reference_id."""
	anchor = order.get("cancelled_at") if reason == "order_cancel_restore" else order.get("created_at")
	end_anchor = (
		order.get("cancelled_at")
		if reason == "order_cancel_restore"
		else (order.get("paid_at") or order.get("created_at"))
	)
	if not anchor:
		raise Fatal(
			_("Order {0} has no time to look for its stock movements around.").format(order.get("number"))
		)
	start = get_datetime(anchor.replace("Z", "+00:00")) - WINDOW_BEFORE
	end = get_datetime((end_anchor or anchor).replace("Z", "+00:00")) + WINDOW_AFTER
	fmt = "%Y-%m-%dT%H:%M:%SZ"
	params = {
		"reason": reason,
		"created_after": start.strftime(fmt),
		"created_before": end.strftime(fmt),
		"limit": 500,
	}
	if order.get("branch_id"):
		params["branch_id"] = order["branch_id"]
	return [m for m in client.iter_list("/stock-movements", params) if m.get("reference_id") == order["id"]]
