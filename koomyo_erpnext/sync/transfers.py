"""stock_transfer.completed -> Stock Entry (Material Transfer) of what arrived.

Docs: docs.koomyo.com/events/stock#stock_transfercompleted, OpenAPI schema StockTransfer.
The event has no branch_id: `from` and `to` each name a branch or a central warehouse.

What arrived (`quantity_received`) moves from the source to the destination Warehouse.
When less arrived than was sent, Koomyo has taken the full amount out of the source, so
the shortfall is booked out of the source too (a Material Issue to the Transfer Loss
account), keeping ERPNext's stock in step with Koomyo's.
"""

import frappe
from frappe import _
from frappe.utils import flt

from koomyo_erpnext.exceptions import Fatal, Skipped
from koomyo_erpnext.sync.common import find_existing, money
from koomyo_erpnext.sync.events import handles
from koomyo_erpnext.sync.stock import (
	STOCK_SCOPES,
	account_setting,
	add_line,
	check_opening_date,
	location_row,
	new_stock_entry,
	submit_entry,
)


@handles("stock_transfer.completed", switch="sync_stock", scopes=STOCK_SCOPES)
def on_transfer_completed(event: dict, connection):
	return book_transfer(event["data"], connection)


def book_transfer(transfer: dict, connection):
	if transfer.get("object") != "stock_transfer" or not transfer.get("id"):
		raise Fatal(_("Koomyo sent something that is not a stock transfer."))
	existing = find_existing("Stock Entry", connection, transfer["id"]) or find_existing(
		"Stock Entry", connection, transfer["id"] + ":shortfall"
	)
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
