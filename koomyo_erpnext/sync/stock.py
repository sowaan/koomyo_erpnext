"""Koomyo stock events -> ERPNext stock documents.

Docs: docs.koomyo.com/events/stock, /guides/stock-purchasing, OpenAPI schemas StockUsage,
Waste, StockMovement, StockCount, StockTransfer, Ingredient.

| Koomyo                      | ERPNext                                                      |
|-----------------------------|--------------------------------------------------------------|
| stock.usage_daily           | Stock Entry, Material Issue, to the Cost of Goods Sold account |
| waste.recorded (ingredient) | Stock Entry, Material Issue, to the Waste Map account of the reason |
| stock.adjusted              | Stock Entry, Material Receipt (+) or Material Issue (-)      |
| stock_count.closed          | Stock Reconciliation to the counted quantities              |
| stock_transfer.completed    | Stock Entry, Material Transfer of what arrived (+ a loss for what did not) |

Ingredients are Items matched by (koomyo_connection, koomyo_id) or, on first sight, by the
Koomyo external reference; otherwise created as stock Items with the mapped UOM.
Quantities leave stock at ERPNext's own valuation; receipts come in at Koomyo's unit cost.
Nothing dated before the connection's Opening Stock Date is booked, so opening stock is
never counted twice.
"""

from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import flt, getdate

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import Fatal, NeedsMapping, Skipped
from koomyo_erpnext.sync.common import (
	branch_row,
	check_company,
	find_existing,
	local_time,
	money,
	posting_date,
	setting,
	stamp,
)
from koomyo_erpnext.sync.events import handles
from koomyo_erpnext.sync.items import get_or_create_item

STOCK_SCOPES = ("stock.read", "refs.write")


def quantity(value, what: str) -> float:
	"""Koomyo quantity text (up to 4 decimals) as the float an ERPNext Float field stores."""
	return flt(str(money(value, what)), 6)


# ------------------------------------------------------------------ handlers


@handles("stock.usage_daily", switch="sync_stock", scopes=STOCK_SCOPES)
def on_usage_daily(event: dict, connection):
	usage = event["data"]
	if usage.get("object") != "stock_usage" or not usage.get("branch_id") or not usage.get("business_date"):
		raise Fatal(_("Koomyo sent something that is not a daily usage."))
	# A day is sent once per branch (docs: events/stock#stockusage_daily), so branch + day is its identity.
	koomyo_id = f"usage:{usage['branch_id']}:{usage['business_date']}"
	existing = find_existing("Stock Entry", connection, koomyo_id)
	if existing:
		return ("Stock Entry", existing)
	from koomyo_erpnext.sync.order_stock import PER_ORDER, effective_mode

	if effective_mode(connection, usage["business_date"]) == PER_ORDER:
		raise Skipped(
			_("Stock Deduction is Per Order on {0}: each order already took its ingredients out.").format(
				usage["business_date"]
			)
		)
	check_opening_date(connection, usage)
	branch = branch_row(connection, usage["branch_id"])
	cogs = account_setting(connection, "cogs_account", _("Cost of Goods Sold Account"))
	issue = new_stock_entry(connection, "Material Issue", usage, koomyo_id, usage["branch_id"])
	issue.remarks = _("Koomyo ingredients used for sales on {0} ({1} orders).").format(
		usage["business_date"], usage.get("orders")
	)
	put_back = []
	for line in usage.get("lines") or []:
		qty = quantity(line.get("quantity_used"), "quantity_used")
		if qty > 0:
			add_line(issue, connection, line, qty, source=branch.warehouse, expense=cogs, branch=branch)
		elif qty < 0:
			# Net of cancelled orders: more was put back than used that day.
			put_back.append((line, -qty))
	result = None
	if issue.items:
		result = submit_entry(issue, "")
	if put_back:
		receipt = new_stock_entry(
			connection, "Material Receipt", usage, koomyo_id + ":returned", usage["branch_id"]
		)
		receipt.remarks = _("Koomyo ingredients put back on {0} (net of cancelled orders).").format(
			usage["business_date"]
		)
		for line, qty in put_back:
			add_line(
				receipt,
				connection,
				line,
				qty,
				target=branch.warehouse,
				rate_text=line.get("unit_cost"),
				expense=cogs,
				branch=branch,
			)
		receipt_result = submit_entry(receipt, "")
		result = result or receipt_result
	if not result:
		raise Skipped(_("No ingredients were used."))
	return result


@handles("waste.recorded", switch="sync_stock", scopes=STOCK_SCOPES)
def on_waste_recorded(event: dict, connection):
	waste = event["data"]
	if waste.get("object") != "waste" or not waste.get("id"):
		raise Fatal(_("Koomyo sent something that is not a waste record."))
	existing = find_existing("Stock Entry", connection, waste["id"])
	if existing:
		return ("Stock Entry", existing)
	check_opening_date(connection, waste)
	ingredient = waste.get("ingredient")
	if not ingredient or not ingredient.get("id"):
		raise Fatal(
			_(
				"Waste {0} ({1}) is a whole menu item. Koomyo does not send which ingredients left stock, so it was "
				"not booked. Book it by hand if needed."
			).format(waste["id"], waste.get("description"))
		)
	warehouse, branch = location_warehouse(connection, waste.get("location"), waste.get("branch_id"))
	expense = waste_account(connection, waste.get("reason"))
	qty = quantity(waste.get("quantity"), "quantity")
	entry = new_stock_entry(connection, "Material Issue", waste, waste["id"], waste.get("branch_id"))
	entry.remarks = _("Koomyo waste ({0}): {1}").format(waste.get("reason"), waste.get("description"))
	line = {
		"ingredient_id": ingredient["id"],
		"ingredient": ingredient.get("name"),
		"unit": ingredient.get("unit"),
	}
	if qty > 0:
		add_line(entry, connection, line, qty, source=warehouse, expense=expense, branch=branch)
	return submit_entry(entry, _("Nothing was wasted."))


@handles("stock.adjusted", switch="sync_stock", scopes=STOCK_SCOPES)
def on_stock_adjusted(event: dict, connection):
	movement = event["data"]
	if movement.get("object") != "stock_movement" or not movement.get("id"):
		raise Fatal(_("Koomyo sent something that is not a stock movement."))
	existing = find_existing("Stock Entry", connection, movement["id"])
	if existing:
		return ("Stock Entry", existing)
	check_opening_date(connection, movement)
	warehouse, branch = location_warehouse(connection, movement.get("location"), event.get("branch_id"))
	change = money(movement.get("quantity_change"), "quantity_change")
	if not change:
		raise Skipped(_("The adjustment did not change the quantity."))
	adjustment = account_setting(connection, "stock_adjustment_account", _("Stock Adjustment Account"))
	purpose = "Material Receipt" if change > 0 else "Material Issue"
	entry = new_stock_entry(connection, purpose, movement, movement["id"], event.get("branch_id"))
	entry.remarks = _("Koomyo stock adjustment: {0}").format(movement.get("reason") or "-")
	if change > 0:
		add_line(
			entry,
			connection,
			movement,
			flt(str(change), 6),
			target=warehouse,
			rate_text=movement.get("unit_cost"),
			expense=adjustment,
			branch=branch,
		)
	else:
		add_line(
			entry,
			connection,
			movement,
			flt(str(-change), 6),
			source=warehouse,
			expense=adjustment,
			branch=branch,
		)
	return submit_entry(entry, "")


@handles("stock_count.closed", switch="sync_stock", scopes=STOCK_SCOPES)
def on_stock_count_closed(event: dict, connection):
	count = event["data"]
	if count.get("object") != "stock_count" or not count.get("id"):
		raise Fatal(_("Koomyo sent something that is not a stock count."))
	existing = find_existing("Stock Reconciliation", connection, count["id"])
	if existing:
		return ("Stock Reconciliation", existing)
	if count.get("status") != "closed":
		raise Skipped(_("Stock count {0} is still open.").format(count["id"]))
	check_opening_date(connection, count)
	branch = branch_row(connection, count.get("branch_id"))
	if not branch.warehouse:
		raise NeedsMapping(_("Stock count {0} has no branch with a warehouse.").format(count["id"]))
	reconciliation = frappe.new_doc("Stock Reconciliation")
	reconciliation.update(
		{
			"company": connection.company,
			"purpose": "Stock Reconciliation",
			"posting_date": posting_date(count),
			"set_posting_time": 1,
			"expense_account": account_setting(
				connection, "stock_adjustment_account", _("Stock Adjustment Account")
			),
			"cost_center": branch.cost_center,
		}
	)
	time = local_time(count.get("closed_at"), connection.koomyo_timezone)
	if time:
		reconciliation.posting_time = time
	stamp(reconciliation, connection, count["id"], count.get("branch_id"))
	for line in count.get("lines") or []:
		reconciliation.append(
			"items",
			{
				"item_code": ingredient_item(connection, line),
				"warehouse": branch.warehouse,
				"qty": quantity(line.get("counted_qty"), "counted_qty"),
				"valuation_rate": flt(str(money(line.get("unit_cost"), "unit_cost")), 6),
			},
		)
	if not reconciliation.items:
		raise Skipped(_("The count has no lines."))
	try:
		reconciliation.insert(ignore_permissions=True)
	except frappe.ValidationError as e:
		if "None of the items have any change" in str(e):
			raise Skipped(_("ERPNext already holds the counted quantities; nothing to change."))
		raise
	reconciliation.submit()
	return ("Stock Reconciliation", reconciliation.name)


# ------------------------------------------------------------------ helpers


def check_opening_date(connection, record: dict):
	start = setting(connection, "stock_start_date", _("Opening Stock Date"))
	if posting_date(record) < getdate(start):
		raise Skipped(
			_("Dated {0}, before the Opening Stock Date {1}: already in the opening stock.").format(
				record.get("business_date"), start
			)
		)


def account_setting(connection, fieldname: str, label: str) -> str:
	account = setting(connection, fieldname, label)
	check_company(connection, "Account", account, label)
	return account


def waste_account(connection, reason: str | None) -> str:
	for row in connection.waste_map:
		if row.reason == reason:
			check_company(connection, "Account", row.expense_account, f"Waste Map row {row.idx}")
			return row.expense_account
	raise NeedsMapping(
		_(
			"Waste reason {0!r} has no account. Add a Waste Map row for it on connection {1}, then press Retry."
		).format(reason, connection.name)
	)


def location_warehouse(connection, location: dict | None, branch_id: str | None):
	"""The ERPNext Warehouse for a Koomyo location (a branch or a central warehouse)."""
	if isinstance(location, dict) and location.get("id"):
		row = location_row(connection, location.get("type"), location["id"])
	else:
		row = branch_row(connection, branch_id)
	if not row.warehouse:
		raise NeedsMapping(
			_("Koomyo location {0} has no Warehouse on connection {1}.").format(
				row.koomyo_branch_id, connection.name
			)
		)
	return row.warehouse, row


def location_row(connection, location_type: str | None, location_id: str):
	if location_type == "warehouse":
		for row in connection.branch_map:
			if row.koomyo_branch_id == location_id:
				check_company(connection, "Warehouse", row.warehouse, f"Branch Map row {row.idx}")
				return row
		raise NeedsMapping(
			_(
				"Koomyo warehouse {0} is not mapped. Add a Branch Map row with Location Type 'Warehouse' and this id on connection {1}, then press Retry."
			).format(location_id, connection.name)
		)
	return branch_row(connection, location_id)


def new_stock_entry(connection, purpose: str, record: dict, koomyo_id: str, branch_id: str | None):
	entry = frappe.new_doc("Stock Entry")
	entry.update(
		{
			"company": connection.company,
			"stock_entry_type": purpose,
			"purpose": purpose,
			"posting_date": posting_date(record),
			"set_posting_time": 1,
		}
	)
	time = local_time(record.get("created_at") or record.get("received_at"), connection.koomyo_timezone)
	if time:
		entry.posting_time = time
	stamp(entry, connection, koomyo_id, branch_id)
	return entry


def add_line(
	entry,
	connection,
	line: dict,
	qty: float,
	*,
	source=None,
	target=None,
	rate_text=None,
	expense=None,
	branch=None,
):
	row = {
		"item_code": ingredient_item(connection, line),
		"qty": qty,
		"s_warehouse": source,
		"t_warehouse": target,
		"expense_account": expense,
		"cost_center": branch.cost_center
		if branch and branch.get("cost_center")
		else connection.default_cost_center,
	}
	if target and rate_text is not None:
		rate = money(rate_text, "unit_cost")
		row["basic_rate"] = flt(str(rate), 6)
		row["set_basic_rate_manually"] = 1
		if not rate:
			row["allow_zero_valuation_rate"] = 1
	entry.append("items", row)


def submit_entry(entry, empty_message: str):
	if not entry.items:
		raise Skipped(empty_message or _("Nothing to book."))
	entry.insert(ignore_permissions=True)
	entry.submit()
	return ("Stock Entry", entry.name)


def ingredient_item(
	connection, line: dict, client: KoomyoClient | None = None, ingredient: dict | None = None
) -> str:
	"""The stock Item for a Koomyo ingredient.

	Matched by (koomyo_connection, koomyo_id); on first sight by the Koomyo external reference
	(GET /ingredients/{id} -> external_refs[<system>] = item code); else created.
	"""
	ingredient_id = line.get("ingredient_id")
	if not ingredient_id:
		raise Fatal(_("Koomyo sent a stock line without an ingredient."))
	existing = find_existing("Item", connection, ingredient_id)
	if existing:
		return existing

	client = client or KoomyoClient.for_connection(connection)
	if ingredient is None:  # the menu loader passes the ingredient it already read
		ingredient = client.get("/ingredients/" + quote(ingredient_id, safe=""))
	if ingredient.get("object") != "ingredient" or ingredient.get("id") != ingredient_id:
		raise Fatal(_("Koomyo answered GET /ingredients/{0} with something else.").format(ingredient_id))

	system = connection.ref_system or "erpnext"
	linked = (ingredient.get("external_refs") or {}).get(system)
	if linked and frappe.db.exists("Item", linked):
		item = frappe.get_doc("Item", linked)
		if item.koomyo_connection and (item.koomyo_connection, item.koomyo_id) != (
			connection.name,
			ingredient_id,
		):
			raise NeedsMapping(
				_(
					"Koomyo links ingredient {0} to Item {1}, but that Item already belongs to connection {2}."
				).format(ingredient.get("name"), linked, item.koomyo_connection)
			)
		if not item.is_stock_item:
			raise NeedsMapping(
				_("Koomyo links ingredient {0} to Item {1}, which is not a stock item.").format(
					ingredient.get("name"), linked
				)
			)
		item.db_set({"koomyo_connection": connection.name, "koomyo_id": ingredient_id})
		return item.name

	uom = uom_for(connection, ingredient.get("unit"))
	group = setting(connection, "ingredient_item_group", _("Ingredient Item Group"))
	code = get_or_create_item(
		connection,
		ingredient_id,
		(ingredient.get("name") or ingredient_id)[:140],
		is_stock_item=1,
		item_group=group,
		uom=uom,
		is_purchase_item=1,
	)
	cost = money(ingredient.get("cost_per_unit") or "0", "cost_per_unit")
	if cost:
		frappe.db.set_value("Item", code, "valuation_rate", flt(str(cost), 6))
	# Store the Item code on the Koomyo ingredient, so the next lookup by reference finds it.
	from koomyo_erpnext.sync.customers import write_reference

	write_reference(connection, client, "ingredient", ingredient, frappe.get_doc("Item", code))
	return code


def uom_for(connection, unit: str | None) -> str:
	for row in connection.uom_map:
		if (row.koomyo_unit or "").strip().lower() == (unit or "").strip().lower():
			return row.uom
	raise NeedsMapping(
		_(
			"Koomyo unit {0!r} has no UOM. Add a UOM Map row for it on connection {1}, then press Retry."
		).format(unit, connection.name)
	)
