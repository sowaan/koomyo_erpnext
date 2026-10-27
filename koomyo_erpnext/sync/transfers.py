"""Stock transfers between places: posted in steps, or in one step when no Transit Warehouse is set.

Docs: docs.koomyo.com/events/stock#stock-transfers, OpenAPI schema StockTransfer and the events
`transfer.sent|received|short_closed|cancelled` and `stock_transfer.completed`. The events have no
branch_id: `from` and `to` each name a branch or a central warehouse.

With a Transit Warehouse on the connection, every event brings the transfer as it stands and
`reconcile` posts whatever of it is not yet booked, so the events can arrive in any order, twice,
or be replaced by `stock_transfer.completed` or a backfill, and nothing is ever doubled:

- sent: a Material Transfer with "Transit" on (ERPNext's Send to Warehouse) from the source into the
  Transit Warehouse. Koomyo's stock left the source at that moment.
- each delivery (`receipts[]`, keyed by the receipt id): ERPNext's Receive at Warehouse
  (`make_stock_in_entry`) of that delivery from transit to the destination. ERPNext itself tracks the
  part still in transit.
- returned (a resolution): a receive-at-warehouse entry from transit back to the source.
- lost (a resolution): a Material Issue from transit to the Transfer Loss Account, with the reason
  and Koomyo's value in the remarks. The stock was already out of the source.
- cancelled: the send is cancelled (the source gets the stock back). If a delivery or a return was
  booked against it, nothing is forced: the event fails with a message for a person.
- a draft creates nothing.

Without a Transit Warehouse the old behaviour stays: `stock_transfer.completed` books what arrived as
one Material Transfer and the shortfall as a loss, and the step events are skipped with a message.
"""

import frappe
from frappe import _
from frappe.utils import flt

from koomyo_erpnext.exceptions import Fatal, Skipped
from koomyo_erpnext.sync.common import (
	check_company,
	find_existing,
	local_date,
	local_time,
	money,
	posting_date,
	stamp,
)
from koomyo_erpnext.sync.events import handles
from koomyo_erpnext.sync.stock import (
	STOCK_SCOPES,
	account_setting,
	add_line,
	check_opening_date,
	ingredient_item,
	location_row,
	new_stock_entry,
	submit_entry,
)

STEP_TYPES = ("transfer.sent", "transfer.received", "transfer.short_closed", "transfer.cancelled")


@handles(*STEP_TYPES, switch="sync_stock", scopes=STOCK_SCOPES)
def on_transfer_step(event: dict, connection):
	transfer = event["data"]
	check_transfer(transfer)
	if not transit_warehouse(connection):
		raise Skipped(
			_(
				"Set Transit Warehouse on connection {0} to post stock transfers in steps. Until then a transfer is booked in one step when it completes (stock_transfer.completed)."
			).format(connection.name)
		)
	return reconcile(transfer, connection, event["type"])


@handles("stock_transfer.completed", switch="sync_stock", scopes=STOCK_SCOPES)
def on_transfer_completed(event: dict, connection):
	transfer = event["data"]
	check_transfer(transfer)
	if transit_warehouse(connection):
		return reconcile(transfer, connection, event["type"])
	return book_transfer(transfer, connection)


def check_transfer(transfer: dict):
	if transfer.get("object") != "stock_transfer" or not transfer.get("id"):
		raise Fatal(_("Koomyo sent something that is not a stock transfer."))


def transit_warehouse(connection) -> str | None:
	warehouse = connection.get("transit_warehouse")
	if warehouse:
		check_company(connection, "Warehouse", warehouse, _("Transit Warehouse"))
	return warehouse


# ------------------------------------------------------------------ the steps


def key(transfer: dict, *parts: str) -> str:
	return ":".join(("transfer", transfer["id"], *parts))


def legacy_entry(transfer: dict, connection):
	"""An entry made before transfers were posted in steps (one entry, keyed by the transfer id)."""
	return find_existing("Stock Entry", connection, transfer["id"]) or find_existing(
		"Stock Entry", connection, transfer["id"] + ":shortfall"
	)


def reconcile(transfer: dict, connection, event_type: str):
	legacy = legacy_entry(transfer, connection)
	if legacy:
		return ("Stock Entry", legacy)
	status = transfer.get("status")
	if status == "cancelled":
		return cancel_send(transfer, connection)
	if status not in ("in_transit", "partial", "received"):
		raise Skipped(_("Transfer {0} is {1}: nothing moved yet.").format(transfer["id"], status))

	places = places_of(transfer, connection)
	when = step_date(transfer, "sent_at", connection)
	check_opening_date(connection, {"business_date": str(when)})
	send_name = find_existing("Stock Entry", connection, key(transfer, "send"))
	last = ("Stock Entry", send_name) if send_name else None
	if not send_name:
		if cancelled_already(transfer, connection):
			raise Skipped(_("Transfer {0} was cancelled; its send is not booked.").format(transfer["id"]))
		last = book_send(transfer, connection, places)
		send_name = last[1]

	for receipt in deliveries(transfer):
		made = book_receipt(transfer, connection, places, send_name, receipt)
		last = made or last
	for resolution in resolutions(transfer):
		made = book_resolution(transfer, connection, places, send_name, resolution)
		last = made or last
	return last


def places_of(transfer: dict, connection):
	source = location_row(
		connection, (transfer.get("from") or {}).get("type"), (transfer.get("from") or {}).get("id")
	)
	target = location_row(
		connection, (transfer.get("to") or {}).get("type"), (transfer.get("to") or {}).get("id")
	)
	if source.warehouse == target.warehouse:
		raise Skipped(
			_("Both ends of transfer {0} map to Warehouse {1}.").format(transfer["id"], source.warehouse)
		)
	return {"source": source, "target": target, "transit": transit_warehouse(connection)}


def step_date(transfer: dict, field: str, connection, text: str | None = None):
	return local_date(text or transfer.get(field), connection.koomyo_timezone) or posting_date(transfer)


def dated_entry(connection, transfer: dict, koomyo_id: str, branch_id, when_text, field: str):
	entry = new_stock_entry(connection, "Material Transfer", transfer, koomyo_id, branch_id)
	entry.posting_date = step_date(transfer, field, connection, when_text)
	time = local_time(when_text or transfer.get(field), connection.koomyo_timezone)
	if time:
		entry.posting_time = time
	return entry


def lines_of(transfer: dict) -> list[dict]:
	return [
		line for line in transfer.get("lines") or [] if money(line.get("quantity_sent"), "quantity_sent") > 0
	]


def book_send(transfer: dict, connection, places) -> tuple:
	entry = dated_entry(
		connection, transfer, key(transfer, "send"), (transfer.get("from") or {}).get("id"), None, "sent_at"
	)
	entry.add_to_transit = 1
	entry.remarks = _("Koomyo transfer {0} sent: {1}").format(
		transfer.get("number") or transfer["id"], transfer.get("notes") or "-"
	)
	for line in lines_of(transfer):
		add_line(
			entry,
			connection,
			line,
			flt(str(money(line["quantity_sent"], "quantity_sent")), 6),
			source=places["source"].warehouse,
			target=places["transit"],
			branch=places["source"],
		)
	return submit_entry(entry, _("Transfer {0} has no lines.").format(transfer["id"]))


def cancelled_already(transfer: dict, connection) -> bool:
	return bool(
		frappe.db.exists(
			"Koomyo Event Log",
			{
				"connection": connection.name,
				"event_type": "transfer.cancelled",
				"entity_id": transfer["id"],
				"status": ("in", ("Done", "Skipped")),
			},
		)
	)


def cancel_send(transfer: dict, connection):
	name = find_existing("Stock Entry", connection, key(transfer, "send"))
	if not name:
		raise Skipped(
			_("Transfer {0} was cancelled; nothing of it was booked in ERPNext.").format(transfer["id"])
		)
	entry = frappe.get_doc("Stock Entry", name)
	if entry.docstatus == 2:
		return ("Stock Entry", name)
	against = frappe.get_all(
		"Stock Entry", filters={"outgoing_stock_entry": name, "docstatus": 1}, pluck="name"
	)
	if against:
		raise Fatal(
			_(
				"Koomyo cancelled transfer {0} but {1} was already booked against its send {2}. Nothing was cancelled: reverse those entries, then press Retry."
			).format(transfer.get("number") or transfer["id"], ", ".join(against), name)
		)
	entry.cancel()
	entry.add_comment(
		"Comment", _("Cancelled in Koomyo. Reason: {0}").format(transfer.get("cancel_reason") or "-")
	)
	return ("Stock Entry", name)


def deliveries(transfer: dict) -> list[dict]:
	"""Each delivery with the lines that arrived in it; an old-shape transfer (no receipts) is one delivery."""
	receipts = [r for r in transfer.get("receipts") or [] if r.get("id")]
	if receipts:
		return receipts
	arrived = [
		l
		for l in transfer.get("lines") or []
		if money(l.get("quantity_received") or "0", "quantity_received") > 0
	]
	if not arrived:
		return []
	return [
		{
			"id": transfer["id"],
			"received_at": transfer.get("received_at"),
			"lines": [
				{"ingredient_id": l.get("ingredient_id"), "quantity": l["quantity_received"], "line": l}
				for l in arrived
			],
		}
	]


def resolutions(transfer: dict) -> list[dict]:
	"""What happened to what did not arrive. An old-shape transfer that ended short books the rest as lost."""
	rows = [r for r in transfer.get("resolutions") or [] if r.get("id")]
	if rows or transfer.get("status") != "received":
		return rows
	rows = []
	for line in transfer.get("lines") or []:
		sent = money(line.get("quantity_sent"), "quantity_sent")
		done = money(line.get("quantity_received") or "0", "quantity_received") + money(
			line.get("quantity_returned") or "0", "quantity_returned"
		)
		lost = money(line.get("quantity_lost") or "0", "quantity_lost")
		left = sent - done - lost
		if left > 0:
			rows.append(
				{
					"id": f"{transfer['id']}:shortfall:{line.get('ingredient_id')}",
					"ingredient_id": line.get("ingredient_id"),
					"kind": "lost",
					"quantity": str(left),
					"unit_cost": line.get("unit_cost"),
					"value": None,
					"reason_code": "other",
					"line": line,
				}
			)
	return rows


def line_for(transfer: dict, ingredient_id: str, hint: dict | None = None) -> dict:
	if hint:
		return hint
	for line in transfer.get("lines") or []:
		if line.get("ingredient_id") == ingredient_id:
			return line
	raise Fatal(_("Transfer {0} has no line for ingredient {1}.").format(transfer["id"], ingredient_id))


def receive_from_transit(
	transfer, connection, places, send_name, koomyo_id, branch_id, when_text, field, wanted, to_warehouse
):
	"""ERPNext's Receive at Warehouse for `wanted` ({item_code: qty}) from the send entry's transit stock."""
	from erpnext.stock.doctype.stock_entry.stock_entry import make_stock_in_entry

	entry = make_stock_in_entry(send_name)
	entry.posting_date = step_date(transfer, field, connection, when_text)
	entry.set_posting_time = 1
	time = local_time(when_text or transfer.get(field), connection.koomyo_timezone)
	if time:
		entry.posting_time = time
	remaining = {}
	for row in list(entry.items):
		qty = wanted.get(row.item_code, 0)
		if qty <= 0:
			entry.remove(row)
			continue
		take = min(qty, flt(row.qty, 6))
		if take < qty - 1e-6:
			raise Fatal(
				_("Transfer {0}: {1} of {2} arrived but only {3} is still in transit on {4}.").format(
					transfer["id"], qty, row.item_code, flt(row.qty, 6), send_name
				)
			)
		wanted[row.item_code] = qty - take
		row.qty = take
		row.t_warehouse = to_warehouse
		row.cost_center = (
			places["target"].cost_center
			if to_warehouse == places["target"].warehouse
			else places["source"].cost_center
		)
		remaining[row.item_code] = take
	if not entry.items:
		raise Skipped(_("Nothing in transit to receive for transfer {0}.").format(transfer["id"]))
	stamp(entry, connection, koomyo_id, branch_id)
	entry.insert(ignore_permissions=True)
	entry.submit()
	return ("Stock Entry", entry.name)


def book_receipt(transfer, connection, places, send_name, receipt) -> tuple | None:
	koomyo_id = key(transfer, "recv", receipt["id"])
	if find_existing("Stock Entry", connection, koomyo_id):
		return None
	wanted: dict[str, float] = {}
	for line in receipt.get("lines") or []:
		item = ingredient_item(connection, line_for(transfer, line.get("ingredient_id"), line.get("line")))
		wanted[item] = wanted.get(item, 0) + flt(str(money(line.get("quantity"), "quantity")), 6)
	return receive_from_transit(
		transfer,
		connection,
		places,
		send_name,
		koomyo_id,
		(transfer.get("to") or {}).get("id"),
		receipt.get("received_at"),
		"received_at",
		wanted,
		places["target"].warehouse,
	)


def book_resolution(transfer, connection, places, send_name, resolution) -> tuple | None:
	koomyo_id = key(transfer, "res", resolution["id"])
	if find_existing("Stock Entry", connection, koomyo_id):
		return None
	quantity = money(resolution.get("quantity"), "quantity")
	if quantity <= 0:
		return None
	line = line_for(transfer, resolution.get("ingredient_id"), resolution.get("line"))
	item = ingredient_item(connection, line)
	when = resolution.get("resolved_at")
	kind = resolution.get("kind")
	if kind == "returned":
		return receive_from_transit(
			transfer,
			connection,
			places,
			send_name,
			koomyo_id,
			(transfer.get("from") or {}).get("id"),
			when,
			"received_at",
			{item: flt(str(quantity), 6)},
			places["source"].warehouse,
		)
	if kind != "lost":
		raise Fatal(_("Transfer {0}: unknown resolution {1!r}.").format(transfer["id"], kind))
	loss_account = account_setting(connection, "transfer_loss_account", _("Transfer Loss Account"))
	entry = new_stock_entry(
		connection, "Material Issue", transfer, koomyo_id, (transfer.get("from") or {}).get("id")
	)
	entry.posting_date = step_date(transfer, "received_at", connection, when)
	time = local_time(when, connection.koomyo_timezone)
	if time:
		entry.posting_time = time
	reason = resolution.get("reason_code") or "other"
	entry.remarks = _("Koomyo transfer {0}: lost in transit ({1}). Koomyo's value: {2}.").format(
		transfer.get("number") or transfer["id"], reason, resolution.get("value") or "-"
	)
	add_line(
		entry,
		connection,
		line,
		flt(str(quantity), 6),
		source=places["transit"],
		expense=loss_account,
		branch=places["source"],
	)
	for row in entry.items:
		row.description = _("Lost in transit ({0}) {1}").format(reason, resolution.get("note") or "")[:140]
	return submit_entry(entry, "")


def fully_booked(transfer: dict, connection) -> bool:
	"""True when every step of the transfer as it stands is already in ERPNext (for the backfill's count)."""
	if legacy_entry(transfer, connection):
		return True
	if transfer.get("status") == "cancelled":
		name = find_existing("Stock Entry", connection, key(transfer, "send"))
		return not name or frappe.db.get_value("Stock Entry", name, "docstatus") == 2
	if not find_existing("Stock Entry", connection, key(transfer, "send")):
		return False
	return all(
		find_existing("Stock Entry", connection, key(transfer, "recv", r["id"])) for r in deliveries(transfer)
	) and all(
		find_existing("Stock Entry", connection, key(transfer, "res", r["id"]))
		for r in resolutions(transfer)
		if money(r.get("quantity"), "quantity") > 0
	)


# ------------------------------------------------------------------ one step (no Transit Warehouse)


def book_transfer(transfer: dict, connection):
	"""stock_transfer.completed as one Material Transfer of what arrived, the shortfall as a loss."""
	check_transfer(transfer)
	existing = legacy_entry(transfer, connection)
	if existing:
		return ("Stock Entry", existing)
	if transfer.get("status") != "received":
		raise Skipped(_("Transfer {0} is {1}, not received.").format(transfer["id"], transfer.get("status")))
	check_opening_date(connection, transfer)

	source = location_row(
		connection, (transfer.get("from") or {}).get("type"), (transfer.get("from") or {}).get("id")
	)
	target = location_row(
		connection, (transfer.get("to") or {}).get("type"), (transfer.get("to") or {}).get("id")
	)
	if source.warehouse == target.warehouse:
		raise Skipped(
			_("Both ends of transfer {0} map to Warehouse {1}.").format(transfer["id"], source.warehouse)
		)
	branch_id = (transfer.get("to") or {}).get("id")

	move = new_stock_entry(connection, "Material Transfer", transfer, transfer["id"], branch_id)
	move.remarks = _("Koomyo transfer: {0}").format(transfer.get("notes") or transfer["id"])
	shortfalls = []
	for line in transfer.get("lines") or []:
		sent = money(line.get("quantity_sent"), "quantity_sent")
		received = money(line.get("quantity_received"), "quantity_received")
		if received < 0 or received > sent:
			raise Fatal(
				_("Transfer {0}: {1} received of {2} sent does not make sense.").format(
					transfer["id"], received, sent
				)
			)
		if received:
			add_line(
				move,
				connection,
				line,
				flt(str(received), 6),
				source=source.warehouse,
				target=target.warehouse,
				branch=target,
			)
		if sent > received:
			shortfalls.append((line, flt(str(sent - received), 6)))

	result = submit_entry(move, "") if move.items else None
	if shortfalls:
		loss_account = account_setting(connection, "transfer_loss_account", _("Transfer Loss Account"))
		loss = new_stock_entry(
			connection, "Material Issue", transfer, transfer["id"] + ":shortfall", branch_id
		)
		loss.remarks = _("Koomyo transfer {0}: sent but not received.").format(transfer["id"])
		for line, qty in shortfalls:
			add_line(
				loss, connection, line, qty, source=source.warehouse, expense=loss_account, branch=source
			)
		loss_result = submit_entry(loss, "")
		result = result or loss_result
	if not result:
		raise Skipped(_("Transfer {0} has no lines.").format(transfer["id"]))
	return result
