"""Physical stock changes made in ERPNext -> Koomyo (scope `stock.write`): adjustments, waste, supply issues, counts.

Docs: OpenAPI POST /stock-adjustments, /waste, /supply-issues (+ /undo), /stock-counts (+ PUT /lines, /close).
Opt in per connection (**Send Stock Changes to Koomyo**, under Sync Stock) and per document: nothing is sent
unless you mark it. **Never sales**: Koomyo takes sales off stock itself, so a Sales Invoice, a Delivery Note
or an unmarked Stock Entry never goes to Koomyo.

- Stock Entry (Material Issue or Receipt) marked **Send To Koomyo As**:
  - *Adjustment* (breakage, found stock): one `POST /stock-adjustments` per row, signed quantity (an issue is
    negative). Cancelling it sends the opposite adjustment (Koomyo has no delete).
  - *Waste*: one `POST /waste`, a line per row; the reason comes from the Waste Map by the row's expense
    account. Koomyo has no undo for waste: cancelling it only leaves a message.
  - *Supply issue*: one `POST /supply-issues` per row (reason `used`; needs the Supplies switch in Koomyo).
    Cancelling it calls `POST /supply-issues/{id}/undo`.
- Stock Reconciliation marked **Send To Koomyo As A Stock Count**: for each warehouse on the Branch Map,
  `POST /stock-counts`, `PUT .../lines` with the counted quantities, `POST .../close`. A closed count is
  locked in Koomyo (409 `count_closed`): cancelling the reconciliation only leaves a message.

Every record carries `external_ref` = the document name (`#row` for a row), so a retry, even after a lost
answer, never makes a second one, and Koomyo's own events for them book nothing (sync/stock.py).
"""

from decimal import Decimal
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import flt

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import Fatal, KoomyoAPIError, NeedsMapping, Skipped
from koomyo_erpnext.sync.events import handles, idempotency_key, koomyo_write, store_outbound, warn
from koomyo_erpnext.sync.purchasing import linked_ingredient
from koomyo_erpnext.sync.transfers_out import one_warehouse, place_of

SCOPES = ("stock.read", "stock.write")
SWITCH = "push_stock_changes"
FOUR = Decimal("0.0001")
WASTE_REASONS = ("staff_meal", "comp", "tasting", "spoilage", "other")
KINDS = {"Adjustment": "adjustment", "Waste": "waste", "Supply issue": "supply"}


def quantity_text(value, what: str) -> str:
	qty = Decimal(str(flt(value, 6)))
	if qty != qty.quantize(FOUR):
		raise Fatal(_("{0}: Koomyo takes four decimals at most, not {1}.").format(what, qty.normalize()))
	return format(qty.quantize(FOUR).normalize(), "f")


def connections_for(company: str) -> list:
	names = frappe.get_all(
		"Koomyo Connection",
		filters={"company": company, "enabled": 1, "sync_stock": 1, SWITCH: 1},
		pluck="name",
	)
	return [frappe.get_cached_doc("Koomyo Connection", n) for n in names]


def row_warehouse(doc, row) -> str | None:
	return row.get("s_warehouse") if doc.purpose == "Material Issue" else row.get("t_warehouse")


# ------------------------------------------------------------------ doc_events


def on_stock_change_submit(doc, method=None):
	if doc.get("purpose") not in ("Material Issue", "Material Receipt"):
		return
	if doc.get("koomyo_connection") or doc.get("koomyo_sent") or doc.get("koomyo_send_as") not in KINDS:
		return
	warehouses = {row_warehouse(doc, row) for row in doc.items}
	for connection in connections_for(doc.company):
		if any(place_of(connection, w) for w in warehouses if w):
			store_outbound(connection, "erpnext.stock_change", doc)
			return
	# Marked, but no connection with this switched on has the warehouse: say so instead of doing nothing.
	frappe.msgprint(
		_(
			"{0} is marked to be sent to Koomyo, but no connection with Send Stock Changes to Koomyo switched on has its warehouse on the Branch Map."
		).format(doc.name),
		indicator="orange",
	)


def on_count_submit(doc, method=None):
	if doc.get("purpose") != "Stock Reconciliation" or not doc.get("koomyo_send") or doc.get("koomyo_sent"):
		return
	warehouses = {row.warehouse for row in doc.items}
	for connection in connections_for(doc.company):
		if any(place_of(connection, w) for w in warehouses if w):
			store_outbound(connection, "erpnext.stock_count", doc)
			return
	frappe.msgprint(
		_(
			"{0} is marked to be sent to Koomyo as a count, but no connection with Send Stock Changes to Koomyo switched on has its warehouse on the Branch Map."
		).format(doc.name),
		indicator="orange",
	)


def before_stock_change_cancel(doc, method=None):
	"""Waste and a closed count cannot be undone in Koomyo, so they cannot be cancelled here either."""
	if doc.flags.get("from_koomyo") or not doc.get("koomyo_sent") or not doc.get("koomyo_connection"):
		return
	if doc.doctype == "Stock Reconciliation":
		frappe.throw(
			_(
				"{0} was sent to Koomyo as a stock count, and a closed count is locked there (it is part of the stock history), so it cannot be cancelled here either. Count again if the quantities were wrong."
			).format(doc.name),
			title=_("Koomyo cannot undo this"),
		)
	if doc.get("purpose") != "Material Transfer" and KINDS.get(doc.get("koomyo_send_as") or "") == "waste":
		frappe.throw(
			_(
				"{0} was sent to Koomyo as waste, and Koomyo has no call to undo waste, so it cannot be cancelled here either."
			).format(doc.name),
			title=_("Koomyo cannot undo this"),
		)


def on_stock_change_cancel(doc, method=None):
	"""A sent Stock Entry or Stock Reconciliation is cancelled in ERPNext: undo what Koomyo can undo."""
	if doc.get("purpose") == "Material Transfer" or doc.flags.get("from_koomyo"):
		return
	if not doc.get("koomyo_sent") or not doc.get("koomyo_connection"):
		return
	connection = frappe.get_cached_doc("Koomyo Connection", doc.koomyo_connection)
	if connection.enabled and connection.sync_stock and connection.get(SWITCH):
		store_outbound(connection, "erpnext.stock_change_cancel", doc)


# ------------------------------------------------------------------ Stock Entry -> Koomyo


@handles("erpnext.stock_change", switch=SWITCH, scopes=SCOPES)
def push_stock_change(event: dict, connection, client: KoomyoClient | None = None):
	doc = frappe.get_doc("Stock Entry", event["data"]["name"])
	kind = KINDS.get(doc.get("koomyo_send_as") or "")
	if doc.docstatus != 1 or not kind:
		raise Skipped(_("{0} is not a submitted entry marked to be sent to Koomyo.").format(doc.name))
	if doc.get("koomyo_sent"):
		raise Skipped(_("{0} is already in Koomyo.").format(doc.name))
	warehouse = one_warehouse(doc, "s_warehouse" if doc.purpose == "Material Issue" else "t_warehouse")
	if not warehouse:
		raise Fatal(
			_(
				"{0} takes stock from or puts it into more than one warehouse. Koomyo takes one place for each record: make one Stock Entry for each warehouse."
			).format(doc.name)
		)
	place = place_of(connection, warehouse)
	if not place:
		raise NeedsMapping(
			_(
				"Warehouse {0} is not on connection {1}'s Branch Map, so Koomyo cannot be told about {2}. Add a row (a branch, or Location Type Warehouse), then press Retry."
			).format(warehouse, connection.name, doc.name)
		)
	if kind != "adjustment" and doc.purpose != "Material Issue":
		raise Fatal(_("{0}: only a Material Issue can be sent as waste or a supply issue.").format(doc.name))
	client = client or KoomyoClient.for_connection(connection)
	{"adjustment": send_adjustments, "waste": send_waste, "supply": send_supplies}[kind](
		connection, client, doc, place, event
	)
	doc.db_set({"koomyo_connection": connection.name, "koomyo_sent": 1}, update_modified=False)
	return ("Stock Entry", doc.name)


def ref(connection, doc, suffix: str) -> dict:
	return {"system": connection.ref_system or "erpnext", "external_id": f"{doc.name}#{suffix}"}


def post(connection, client, doc, path: str, body: dict, action: str, event: dict) -> dict:
	return koomyo_write(
		connection,
		client,
		"POST",
		path,
		body,
		idempotency_key(doc.doctype, doc.name, doc.modified, f"{action}-{event.get('attempt', 0)}"),
	)


def remember(row, record_id: str):
	row.db_set("koomyo_line_id", record_id, update_modified=False)


def send_adjustments(connection, client, doc, place, event):
	sign = -1 if doc.purpose == "Material Issue" else 1
	for row in doc.items:
		if row.get("koomyo_line_id"):
			continue  # sent on an earlier try
		qty = Decimal(str(flt(row.transfer_qty, 6))) * sign
		answer = post(
			connection,
			client,
			doc,
			"/stock-adjustments",
			{
				"place": {"type": place[0], "id": place[1]},
				"ingredient_id": linked_ingredient(row.item_code, connection),
				"quantity": quantity_text(qty, row.item_code),
				"reason": _("ERPNext {0}").format(doc.name)[:100],
				"note": (doc.get("remarks") or "")[:300] or None,
				"external_ref": ref(connection, doc, str(row.idx)),
			},
			f"adjust-{row.idx}",
			event,
		)
		if not answer.get("id"):
			raise Fatal(_("Koomyo answered the adjustment of {0} with something else.").format(row.item_code))
		remember(row, answer["id"])
		if answer.get("below_zero"):
			warn(
				_("Koomyo took {0} below zero at that place: it had already been used.").format(row.item_code)
			)


def waste_reason(connection, doc) -> str:
	reasons = set()
	for row in doc.items:
		match = [m.reason for m in connection.waste_map if m.expense_account == row.get("expense_account")]
		if not match:
			raise NeedsMapping(
				_(
					"{0}: the expense account {1} is not on connection {2}'s Waste Map, so Koomyo cannot be told the waste reason. Add a Waste Map row for it, then press Retry."
				).format(doc.name, row.get("expense_account") or "-", connection.name)
			)
		reasons.add(match[0])
	if len(reasons) != 1:
		raise Fatal(
			_(
				"{0} has rows with different waste reasons ({1}). Koomyo takes one reason for each waste: make one Stock Entry for each reason."
			).format(doc.name, ", ".join(sorted(reasons)))
		)
	(reason,) = reasons
	if reason not in WASTE_REASONS:
		raise NeedsMapping(
			_("Waste Map reason {0!r} is not one Koomyo takes ({1}).").format(
				reason, ", ".join(WASTE_REASONS)
			)
		)
	return reason


def send_waste(connection, client, doc, place, event):
	if all(row.get("koomyo_line_id") for row in doc.items):
		return
	lines = [
		{
			"ingredient_id": linked_ingredient(row.item_code, connection),
			"quantity": quantity_text(row.transfer_qty, row.item_code),
			"description": (row.get("description") or row.item_code)[:140],
			"external_ref": ref(connection, doc, str(row.idx)),
		}
		for row in doc.items
	]
	body = {
		"place": {"type": place[0], "id": place[1]},
		"reason": waste_reason(connection, doc),
		"note": (doc.get("remarks") or "")[:300] or None,
		"lines": lines,
	}
	try:
		answer = post(connection, client, doc, "/waste", body, "waste", event)
	except KoomyoAPIError as e:
		if e.code == "supply_not_allowed":
			raise Fatal(
				_(
					"Koomyo refused {0} as waste: one of its items is a supply (soap, gloves), not food or packaging. Send it as a Supply issue instead."
				).format(doc.name)
			)
		raise
	made = answer.get("data") if answer.get("object") == "list" else [answer]
	by_row = {}
	for waste in made or []:
		suffix = ((waste.get("external_refs") or {}).get(connection.ref_system or "erpnext") or "").split("#")
		if len(suffix) == 2 and waste.get("id"):
			by_row[suffix[1]] = waste["id"]
	for row in doc.items:
		if str(row.idx) in by_row:
			remember(row, by_row[str(row.idx)])
	if len(by_row) != len(doc.items):
		raise Fatal(_("Koomyo did not answer one waste for each row of {0}.").format(doc.name))


def send_supplies(connection, client, doc, place, event):
	for row in doc.items:
		if row.get("koomyo_line_id"):
			continue
		try:
			answer = post(
				connection,
				client,
				doc,
				"/supply-issues",
				{
					"place": {"type": place[0], "id": place[1]},
					"ingredient_id": linked_ingredient(row.item_code, connection),
					"quantity": quantity_text(row.transfer_qty, row.item_code),
					"reason": "used",
					"note": (doc.get("remarks") or "")[:300] or None,
					"external_ref": ref(connection, doc, str(row.idx)),
				},
				f"supply-{row.idx}",
				event,
			)
		except KoomyoAPIError as e:
			if e.code == "feature_not_available":
				raise Fatal(
					_(
						"Koomyo refused {0}: the Supplies switch is off for this restaurant. Turn it on in Koomyo, then press Retry."
					).format(doc.name)
				)
			raise
		if not answer.get("id"):
			raise Fatal(
				_("Koomyo answered the supply issue of {0} with something else.").format(row.item_code)
			)
		remember(row, answer["id"])
		if answer.get("below_zero"):
			warn(
				_("Koomyo took {0} below zero at that place: it had already been used.").format(row.item_code)
			)


# ------------------------------------------------------------------ Stock Reconciliation -> a count


@handles("erpnext.stock_count", switch=SWITCH, scopes=SCOPES)
def push_stock_count(event: dict, connection, client: KoomyoClient | None = None):
	doc = frappe.get_doc("Stock Reconciliation", event["data"]["name"])
	if doc.docstatus != 1 or doc.purpose != "Stock Reconciliation" or not doc.get("koomyo_send"):
		raise Skipped(_("{0} is not a submitted count marked to be sent to Koomyo.").format(doc.name))
	if doc.get("koomyo_sent"):
		raise Skipped(_("{0} is already in Koomyo.").format(doc.name))
	client = client or KoomyoClient.for_connection(connection)
	by_warehouse: dict[str, list] = {}
	left_out = 0
	for row in doc.items:
		if not place_of(connection, row.warehouse):
			left_out += 1  # not a Koomyo place: not Koomyo's stock
			continue
		by_warehouse.setdefault(row.warehouse, []).append(row)
	if not by_warehouse:
		raise NeedsMapping(
			_(
				"No warehouse of {0} is on connection {1}'s Branch Map, so Koomyo cannot be told about the count. Add a row, then press Retry."
			).format(doc.name, connection.name)
		)
	for warehouse, rows in by_warehouse.items():
		send_count(connection, client, doc, warehouse, rows, event)
	if left_out:
		warn(_("{0} row(s) of other warehouses were left out: they are not Koomyo places.").format(left_out))
	doc.db_set({"koomyo_connection": connection.name, "koomyo_sent": 1}, update_modified=False)
	return ("Stock Reconciliation", doc.name)


def send_count(connection, client, doc, warehouse: str, rows: list, event: dict):
	kind, place_id = place_of(connection, warehouse)
	count = post(
		connection,
		client,
		doc,
		"/stock-counts",
		{
			"place": {"type": kind, "id": place_id},
			"notes": _("ERPNext {0}").format(doc.name)[:300],
			"external_ref": {
				"system": connection.ref_system or "erpnext",
				"external_id": f"{doc.name}#{warehouse}"[:200],
			},
		},
		f"count-{warehouse}",
		event,
	)
	if not count.get("id"):
		raise Fatal(_("Koomyo answered the count with something else."))
	if count.get("status") == "closed":
		return  # an earlier try already closed it
	merged: dict[str, Decimal] = {}
	for row in rows:
		ingredient = linked_ingredient(row.item_code, connection)
		merged[ingredient] = Decimal(str(flt(row.qty, 6)))
	path = f"/stock-counts/{quote(count['id'], safe='')}"
	try:
		koomyo_write(
			connection,
			client,
			"PUT",
			f"{path}/lines",
			{
				"lines": [
					{"ingredient_id": i, "counted_quantity": quantity_text(q, doc.name)}
					for i, q in merged.items()
				]
			},
			idempotency_key(
				doc.doctype, doc.name, doc.modified, f"count-lines-{warehouse}-{event.get('attempt', 0)}"
			),
		)
		post(connection, client, doc, f"{path}/close", {}, f"count-close-{warehouse}", event)
	except KoomyoAPIError as e:
		if e.code == "already_closed":
			return
		if e.code == "count_closed":
			return  # closed meanwhile (by an earlier try): its counted quantities are Koomyo's now
		if e.code == "line_not_found":
			raise Fatal(
				_(
					"Koomyo refused the count of {0}: an ingredient is not on the count (an inactive ingredient?). Nothing was saved; the count stays open in Koomyo."
				).format(doc.name)
			)
		raise


# ------------------------------------------------------------------ cancel


@handles("erpnext.stock_change_cancel", switch=SWITCH, scopes=SCOPES)
def push_stock_change_cancel(event: dict, connection, client: KoomyoClient | None = None):
	doctype = event["data"]["doctype"]
	doc = frappe.get_doc(doctype, event["data"]["name"])
	if doc.docstatus != 2 or not doc.get("koomyo_sent"):
		raise Skipped(_("{0} is not a cancelled document that was sent to Koomyo.").format(doc.name))
	if doctype == "Stock Reconciliation":
		raise Skipped(
			_(
				"{0} was cancelled in ERPNext, but a closed count is locked in Koomyo (it is part of the stock history). Count again there if the quantities were wrong."
			).format(doc.name)
		)
	kind = KINDS.get(doc.get("koomyo_send_as") or "")
	client = client or KoomyoClient.for_connection(connection)
	if kind == "supply":
		return undo_supplies(connection, client, doc, event)
	if kind == "adjustment":
		return reverse_adjustments(connection, client, doc, event)
	raise Skipped(
		_(
			"{0} was cancelled in ERPNext, but Koomyo has no call to undo waste. Correct that stock in Koomyo by hand."
		).format(doc.name)
	)


def undo_supplies(connection, client, doc, event):
	for row in doc.items:
		first = (row.get("koomyo_line_id") or "").split()
		if not first:
			continue
		try:
			answer = post(
				connection,
				client,
				doc,
				f"/supply-issues/{quote(first[0], safe='')}/undo",
				{"note": _("Cancelled in ERPNext ({0})").format(doc.name)[:300]},
				f"undo-{row.idx}",
				event,
			)
		except KoomyoAPIError as e:
			if e.code == "already_undone":
				continue
			raise
		if answer.get("reversal_id"):
			remember(row, f"{first[0]} {answer['reversal_id']}")
	return ("Stock Entry", doc.name)


def reverse_adjustments(connection, client, doc, event):
	"""Koomyo has no delete: the opposite adjustment puts the physical stock back."""
	warehouse = one_warehouse(doc, "s_warehouse" if doc.purpose == "Material Issue" else "t_warehouse")
	place = place_of(connection, warehouse)
	if not place:
		raise NeedsMapping(
			_("Warehouse {0} is not on connection {1}'s Branch Map.").format(warehouse, connection.name)
		)
	sign = 1 if doc.purpose == "Material Issue" else -1  # the opposite of the original
	for row in doc.items:
		if not row.get("koomyo_line_id"):
			continue
		post(
			connection,
			client,
			doc,
			"/stock-adjustments",
			{
				"place": {"type": place[0], "id": place[1]},
				"ingredient_id": linked_ingredient(row.item_code, connection),
				"quantity": quantity_text(Decimal(str(flt(row.transfer_qty, 6))) * sign, row.item_code),
				"reason": _("ERPNext {0} cancelled").format(doc.name)[:100],
				"external_ref": ref(connection, doc, f"{row.idx}-cancel"),
			},
			f"reverse-{row.idx}",
			event,
		)
	return ("Stock Entry", doc.name)
