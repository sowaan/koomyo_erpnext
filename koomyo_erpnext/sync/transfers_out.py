"""ERPNext Stock Entries (Material Transfer) -> Koomyo stock transfers (scope `transfers.write`).

Docs: docs.koomyo.com stock transfers, OpenAPI POST /stock-transfers, `/receive`, `/resolve`, `/cancel`.
Opt in per connection (**Send Stock Transfers to Koomyo**, under Sync Stock). Only warehouses that are on the
connection's Branch Map are Koomyo's business; any other Material Transfer is left alone.

- One step (a plain Material Transfer, stock moved at once in ERPNext): `POST /stock-transfers` (sent at once,
  the stock leaves the source), then `POST .../receive` for every line, so Koomyo's stock matches ERPNext's.
- In steps (Transit Warehouse, ERPNext's Send to Warehouse and Receive at Warehouse): ERPNext does not know
  the destination when it sends (it is chosen when the stock is received), so nothing goes to Koomyo at the
  send. The first Receive at Warehouse makes the transfer in Koomyo (what was sent) and receives it (what
  arrived); a later Receive at Warehouse is a later delivery. What did not arrive stays in transit in both.
- A Receive at Warehouse that puts the stock back at the place it was sent from is the resolution
  `returned`. A loss has no ERPNext document that names its transfer, so it is decided in Koomyo and booked
  here by the inbound sync (`sync/transfers.py`).
- Cancelling the send in ERPNext cancels the transfer in Koomyo (only while nothing arrived there); a
  delivery cannot be undone in Koomyo, so cancelling one only leaves a note.
- Drafts are not sent: an ERPNext draft Stock Entry moves nothing, and so makes nothing in Koomyo.

The echo is never booked twice: the Stock Entry's name is the transfer's `reference` and `external_ref`,
each delivery's `note` names its entry, and the entries carry the keys the inbound sync looks for.
"""

from decimal import Decimal
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import flt

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import Fatal, KoomyoAPIError, NeedsMapping, Skipped
from koomyo_erpnext.sync.events import handles, idempotency_key, koomyo_write, store_outbound
from koomyo_erpnext.sync.purchasing import connections_for_company, linked_ingredient

SCOPES = ("stock.read", "transfers.write")
SWITCH = "push_stock_transfers"
THREE = Decimal("0.001")


def quantity_text(value, what: str) -> str:
	qty = Decimal(str(flt(value, 6)))
	if qty != qty.quantize(THREE):
		raise Fatal(_("{0}: Koomyo takes three decimals at most, not {1}.").format(what, qty.normalize()))
	return format(qty.quantize(THREE), "f")


def place_of(connection, warehouse: str | None) -> tuple[str, str] | None:
	for row in connection.branch_map:
		if row.warehouse == warehouse:
			return ("warehouse" if row.location_type == "Warehouse" else "branch", row.koomyo_branch_id)
	return None


def one_warehouse(doc, field: str) -> str | None:
	"""The single warehouse a Stock Entry takes from (s_warehouse) or puts into (t_warehouse)."""
	found = {row.get(field) for row in doc.items if row.get(field)}
	return next(iter(found)) if len(found) == 1 else None


def key_parts(koomyo_id: str | None) -> tuple[str, str, str] | None:
	"""('send'|'recv'|'res', transfer id, detail) of `transfer:<id>:<step>[:<detail>]`, else None."""
	if koomyo_id and koomyo_id.startswith("transfer:"):
		pieces = koomyo_id.split(":", 3)
		if len(pieces) >= 3:
			return (pieces[2], pieces[1], pieces[3] if len(pieces) > 3 else "")
	return None


# ------------------------------------------------------------------ doc_events


def transfer_connection(doc):
	"""The connection whose Branch Map holds a warehouse of this Material Transfer and has this switched on."""
	warehouses = {row.get("s_warehouse") for row in doc.items} | {row.get("t_warehouse") for row in doc.items}
	if doc.get("outgoing_stock_entry"):
		warehouses |= {
			w
			for w in frappe.get_all(
				"Stock Entry Detail", filters={"parent": doc.outgoing_stock_entry}, pluck="s_warehouse"
			)
		}
	for connection in connections_for_stock(doc.company):
		if any(place_of(connection, w) for w in warehouses if w):
			return connection
	return None


def connections_for_stock(company: str) -> list:
	names = frappe.get_all(
		"Koomyo Connection",
		filters={"company": company, "enabled": 1, "sync_stock": 1, SWITCH: 1},
		pluck="name",
	)
	return [frappe.get_cached_doc("Koomyo Connection", n) for n in names]


def on_stock_entry_submit(doc, method=None):
	"""A submitted Material Transfer that this app did not make from Koomyo: tell Koomyo (one step or a receive)."""
	if doc.get("purpose") != "Material Transfer" or doc.get("koomyo_connection") or doc.get("koomyo_sent"):
		return
	if doc.get("add_to_transit") and not doc.get("outgoing_stock_entry"):
		return  # the send of a transfer in steps: the destination is chosen when it is received
	connection = transfer_connection(doc)
	if connection:
		store_outbound(connection, "erpnext.stock_transfer", doc)


def before_stock_entry_submit(doc, method=None):
	"""Ask Koomyo before a transfer that fulfils purchase request lines is submitted.

	A pre-check only (Koomyo's answer on the push is the final word and its message is shown): the line must be
	approved, ordered or partly received, the quantity must still be needed, and the destination must be the
	request's branch. If Koomyo cannot be read now, a warning is shown and nothing is blocked.
	"""
	if doc.get("purpose") != "Material Transfer" or doc.get("koomyo_connection"):
		return
	if doc.get("outgoing_stock_entry") is None and doc.get("add_to_transit"):
		return  # the destination is not known until the stock is received
	by_request: dict[str, tuple] = {}
	for row in doc.items:
		name = row.get("material_request")
		if not name or not row.get("material_request_item"):
			continue
		mr = frappe.db.get_value(
			"Material Request", name, ["koomyo_connection", "koomyo_id", "docstatus"], as_dict=True
		)
		if not mr or not mr.koomyo_connection or not mr.koomyo_id or mr.docstatus != 1:
			continue
		connection = usable_connection(mr.koomyo_connection)
		if connection and request_line_of(connection, row):
			by_request.setdefault(mr.koomyo_id, (connection, []))[1].append(row)
	for request_id, (connection, rows) in by_request.items():
		problems = precheck_request(connection, request_id, rows)
		if problems:
			frappe.throw("<br>".join(problems), title=_("Koomyo would refuse this transfer"))


def usable_connection(name: str):
	if not frappe.db.exists("Koomyo Connection", name):
		return None
	connection = frappe.get_cached_doc("Koomyo Connection", name)
	ok = connection.enabled and connection.sync_stock and connection.get(SWITCH)
	return connection if ok else None


def precheck_request(connection, request_id: str, rows: list) -> list[str]:
	from koomyo_erpnext.sync.purchase_requests import ORDERABLE

	try:
		card = KoomyoClient.for_connection(connection).get(f"/purchase-requests/{quote(request_id, safe='')}")
	except KoomyoAPIError as e:
		frappe.msgprint(
			_(
				"Koomyo could not be asked about the purchase request ({0}); it will check when the transfer is sent."
			).format(e.code),
			indicator="orange",
		)
		return []
	lines = {l.get("id"): l for l in card.get("lines") or []}
	problems = []
	for row in rows:
		line = lines.get(request_line_of(connection, row))
		if not line:
			problems.append(
				_("Row {0} ({1}): Koomyo has no such request line.").format(row.idx, row.item_code)
			)
			continue
		if line.get("status") not in ORDERABLE:
			problems.append(
				_(
					"Row {0} ({1}): the request line is {2} in Koomyo: only approved, ordered or partly received lines can be fulfilled."
				).format(row.idx, row.item_code, line.get("status"))
			)
			continue
		asked = (
			line.get("approved_quantity")
			if line.get("approved_quantity") is not None
			else line.get("quantity")
		)
		needed = Decimal(str(asked or 0)) - Decimal(str(line.get("ordered_quantity") or 0))
		qty = Decimal(str(flt(row.transfer_qty, 6)))
		if qty > needed:
			problems.append(
				_("Row {0} ({1}): {2} is more than Koomyo still needs for this line ({3}).").format(
					row.idx, row.item_code, qty.normalize(), max(needed, Decimal(0)).normalize()
				)
			)
		place = place_of(connection, row.get("t_warehouse"))
		if card.get("branch_id") and place != ("branch", card["branch_id"]):
			problems.append(
				_(
					"Row {0} ({1}): the transfer must go to the request's branch (Warehouse {2} is not that branch on the Branch Map)."
				).format(row.idx, row.item_code, row.get("t_warehouse"))
			)
	return problems


def on_stock_entry_cancel(doc, method=None):
	"""A Stock Entry that was sent to Koomyo is cancelled in ERPNext (not one Koomyo cancelled itself)."""
	if doc.get("purpose") != "Material Transfer" or doc.flags.get("from_koomyo"):
		return
	if not doc.get("koomyo_sent") or not doc.get("koomyo_connection"):
		return
	connection = frappe.get_cached_doc("Koomyo Connection", doc.koomyo_connection)
	if connection.enabled and connection.sync_stock and connection.get(SWITCH):
		store_outbound(connection, "erpnext.stock_transfer_cancel", doc)


# ------------------------------------------------------------------ send / receive / resolve


@handles("erpnext.stock_transfer", switch=SWITCH, scopes=SCOPES)
def push_stock_transfer(event: dict, connection, client: KoomyoClient | None = None):
	doc = frappe.get_doc("Stock Entry", event["data"]["name"])
	if doc.docstatus != 1 or doc.purpose != "Material Transfer":
		raise Skipped(_("{0} is not a submitted Material Transfer any more.").format(doc.name))
	if doc.get("koomyo_sent"):
		raise Skipped(_("{0} is already in Koomyo.").format(doc.name))
	client = client or KoomyoClient.for_connection(connection)
	if doc.get("outgoing_stock_entry"):
		return receive_step(connection, client, doc, event)
	return one_step(connection, client, doc, event)


def places_for(connection, source_w: str | None, target_w: str | None, doc):
	if not source_w or not target_w:
		raise Fatal(
			_(
				"{0} takes stock from or puts it into more than one warehouse. Koomyo takes one place on each side: make one Stock Entry for each pair."
			).format(doc.name)
		)
	places = []
	for warehouse in (source_w, target_w):
		place = place_of(connection, warehouse)
		if not place:
			raise NeedsMapping(
				_(
					"Warehouse {0} is not on connection {1}'s Branch Map, so Koomyo cannot be told about {2}. Add a row (a branch, or Location Type Warehouse), then press Retry."
				).format(warehouse, connection.name, doc.name)
			)
		places.append(place)
	if places[0] == places[1]:
		raise Skipped(_("Both warehouses of {0} are the same place in Koomyo.").format(doc.name))
	return places


def request_line_of(connection, row) -> str | None:
	"""The Koomyo purchase request line a row fulfils: its Material Request row's `koomyo_line_id`.

	Only for a Material Request of this connection (the row's `material_request_item` link).
	"""
	if not row.get("material_request_item"):
		return None
	found = frappe.db.get_value(
		"Material Request Item", row.material_request_item, ["koomyo_line_id", "parent"], as_dict=True
	)
	if not found or not found.koomyo_line_id:
		return None
	if frappe.db.get_value("Material Request", found.parent, "koomyo_connection") != connection.name:
		return None
	return found.koomyo_line_id


def body_lines(connection, rows) -> list[dict]:
	"""One transfer line per ingredient, and per request line when a row fulfils one (a repeated one is added up)."""
	merged: dict[tuple[str, str | None], Decimal] = {}
	for row in rows:
		key = (linked_ingredient(row.item_code, connection), request_line_of(connection, row))
		merged[key] = merged.get(key, Decimal(0)) + Decimal(str(flt(row.transfer_qty, 6)))
	lines = []
	for (ingredient, request_line), qty in merged.items():
		line = {
			"ingredient_id": ingredient,
			"quantity": quantity_text(qty, _("Quantity of {0}").format(ingredient)),
		}
		if request_line:
			line["request_line_id"] = request_line
		lines.append(line)
	return lines


REQUEST_REFUSALS = ("request_line_not_orderable", "request_quantity_exceeded", "feature_not_available")


def request_refusal(error, doc, has_request: bool) -> str | None:
	"""A plain sentence for a refusal about a purchase request line; Koomyo's own message is always added."""
	said = _("Koomyo said: {0}").format(error.message or error.code)
	if error.code == "request_line_not_orderable":
		return _(
			"Koomyo refused {0}: the purchase request line it fulfils is not approved, ordered or partly received (it may be waiting, rejected, cancelled or already complete). Nothing was made in Koomyo. {1}"
		).format(doc.name, said)
	if error.code == "request_quantity_exceeded":
		return _(
			"Koomyo refused {0}: the purchase request line needs less than the quantity sent (purchase orders and other transfers already count against it). Lower the quantity, then press Retry. Nothing was made in Koomyo. {1}"
		).format(doc.name, said)
	if error.code == "feature_not_available" and has_request:
		return _(
			"Koomyo refused {0}: purchase requests are switched off for this restaurant, so a transfer cannot fulfil a request line. Nothing was made in Koomyo. {1}"
		).format(doc.name, said)
	if error.code == "invalid_parameter" and has_request:
		return _(
			"Koomyo refused {0}: the request line is for another ingredient, or its request belongs to a different branch than the transfer goes to. Nothing was made in Koomyo. {1}"
		).format(doc.name, said)
	return None


def create_transfer(connection, client, sent_entry, from_place, to_place, lines, event) -> dict:
	"""POST /stock-transfers (sent at once). The entry's name is both the reference and the external_ref."""
	body = {
		"from": {"type": from_place[0], "id": from_place[1]},
		"to": {"type": to_place[0], "id": to_place[1]},
		"lines": lines,
		"reference": sent_entry.name[:60],
		"expected_date": str(sent_entry.posting_date),
		"notes": _("ERPNext {0}: {1}").format(sent_entry.name, sent_entry.get("remarks") or "-")[:500],
		"external_ref": {"system": connection.ref_system or "erpnext", "external_id": sent_entry.name},
	}
	try:
		answer = koomyo_write(
			connection,
			client,
			"POST",
			"/stock-transfers",
			body,
			idempotency_key(
				sent_entry.doctype,
				sent_entry.name,
				sent_entry.modified,
				f"transfer-{event.get('attempt', 0)}",
			),
		)
	except KoomyoAPIError as e:
		said = request_refusal(e, sent_entry, any(l.get("request_line_id") for l in lines))
		if said:
			raise Fatal(said)
		if e.code == "invalid_parameter" and "ingredient" in (e.message or "").lower():
			raise Fatal(not_an_ingredient(connection, sent_entry))
		if e.code == "reference_in_use":
			raise Fatal(
				_(
					"Koomyo already has a transfer with the reference {0}, which is not this entry's. Rename the Stock Entry's number series or find that transfer in Koomyo."
				).format(sent_entry.name)
			)
		raise
	if answer.get("object") != "stock_transfer" or not answer.get("id"):
		raise Fatal(_("Koomyo answered the transfer with something else."))
	note_on_requests(connection, sent_entry, answer)
	return answer


def note_on_requests(connection, entry, transfer: dict):
	"""Record the Stock Entry and the Koomyo transfer number on each Koomyo Material Request it fulfils."""
	seen = set()
	for row in entry.items:
		name = row.get("material_request")
		if not name or name in seen or not request_line_of(connection, row):
			continue
		seen.add(name)
		text = _(
			"Stock Entry {0} fulfils request line(s) of this Material Request: Koomyo transfer {1}."
		).format(entry.name, transfer.get("number") or transfer["id"])
		if not frappe.db.exists(
			"Comment", {"reference_doctype": "Material Request", "reference_name": name, "content": text}
		):
			frappe.get_doc("Material Request", name).add_comment("Comment", text)


def not_an_ingredient(connection, doc) -> str:
	"""Koomyo does not know an Item's id as an ingredient: say which Item (often a menu item's id)."""
	bad = [
		f"{r.item_code} ({frappe.db.get_value('Item', r.item_code, 'item_name')})"
		for r in doc.items
		if not frappe.db.get_value("Item", r.item_code, "koomyo_kind")
	] or [r.item_code for r in doc.items]
	return _(
		"Koomyo does not know the Koomyo id of {0} as an ingredient (on {1}). The Item is probably a menu item, not an ingredient. Use the Item made from the ingredient, or link it. Nothing was changed in Koomyo."
	).format(", ".join(bad), doc.name)


def line_ids(transfer: dict) -> dict[str, str]:
	return {l["ingredient_id"]: l["id"] for l in transfer.get("lines") or []}


def receive_rows(connection, transfer: dict, rows, doc) -> list[dict]:
	ids = line_ids(transfer)
	merged: dict[str, Decimal] = {}
	for row in rows:
		ingredient = linked_ingredient(row.item_code, connection)
		if ingredient not in ids:
			raise Fatal(
				_("{0}: {1} is not on Koomyo transfer {2}.").format(
					doc.name, row.item_code, transfer.get("number") or transfer["id"]
				)
			)
		merged[ids[ingredient]] = merged.get(ids[ingredient], Decimal(0)) + Decimal(
			str(flt(row.transfer_qty, 6))
		)
	return [{"line_id": i, "quantity": quantity_text(q, doc.name)} for i, q in merged.items()]


def post_receive(connection, client, transfer: dict, lines: list[dict], doc, event) -> dict:
	try:
		return koomyo_write(
			connection,
			client,
			"POST",
			f"/stock-transfers/{quote(transfer['id'], safe='')}/receive",
			{"lines": lines, "note": _("ERPNext {0}").format(doc.name)[:200]},
			idempotency_key(doc.doctype, doc.name, doc.modified, f"receive-{event.get('attempt', 0)}"),
		)
	except KoomyoAPIError as e:
		explain_refusal(e, doc, transfer)
		raise


def explain_refusal(error, doc, transfer: dict):
	number = transfer.get("number") or transfer["id"]
	if error.code == "exceeds_in_transit":
		raise Fatal(
			_(
				"Koomyo refused {0}: more arrived than is still on its way for transfer {1} (some was already received or resolved there). Nothing was changed in Koomyo."
			).format(doc.name, number)
		)
	if error.code in ("transfer_locked", "transfer_not_sent"):
		raise Fatal(
			_(
				"Koomyo refused {0}: transfer {1} is finished or was never sent (a draft or cancelled transfer takes no delivery)."
			).format(doc.name, number)
		)


def new_ids(before: dict, after: dict, field: str) -> list[str]:
	seen = {r.get("id") for r in before.get(field) or []}
	return [r["id"] for r in after.get(field) or [] if r.get("id") and r["id"] not in seen]


def mark(doc, connection, koomyo_id: str):
	doc.db_set(
		{"koomyo_connection": connection.name, "koomyo_id": koomyo_id, "koomyo_sent": 1},
		update_modified=False,
	)


def one_step(connection, client, doc, event):
	"""A plain Material Transfer: sent at once, then received in full. Resumable: a retry finds the transfer."""
	source_w, target_w = one_warehouse(doc, "s_warehouse"), one_warehouse(doc, "t_warehouse")
	from_place, to_place = places_for(connection, source_w, target_w, doc)
	if doc.get("koomyo_id") and doc.get("koomyo_connection") == connection.name:
		transfer = client.get(f"/stock-transfers/{quote(doc.koomyo_id, safe='')}")  # made, not yet received
	else:
		transfer = create_transfer(
			connection, client, doc, from_place, to_place, body_lines(connection, doc.items), event
		)
		doc.db_set({"koomyo_connection": connection.name, "koomyo_id": transfer["id"]}, update_modified=False)
	opened = [l for l in transfer.get("lines") or [] if Decimal(str(l.get("quantity_open") or 0)) > 0]
	if opened:
		lines = [
			{"line_id": l["id"], "quantity": quantity_text(l["quantity_open"], doc.name)} for l in opened
		]
		post_receive(connection, client, transfer, lines, doc, event)
	mark(doc, connection, transfer["id"])
	return ("Stock Entry", doc.name)


def receive_step(connection, client, doc, event):
	"""Receive at Warehouse: make the transfer in Koomyo on the first one, receive (or return) this delivery."""
	sent = frappe.get_doc("Stock Entry", doc.outgoing_stock_entry)
	if sent.docstatus != 1 or not sent.get("add_to_transit"):
		raise Skipped(_("{0} does not receive a Send to Warehouse entry.").format(doc.name))
	if not connection.get("transit_warehouse") or sent.to_warehouse != connection.transit_warehouse:
		raise NeedsMapping(
			_(
				"Set Transit Warehouse on connection {0} to {1}, the warehouse {2} sent the stock to, so the steps of a transfer match in Koomyo."
			).format(connection.name, sent.to_warehouse, sent.name)
		)
	source_w, target_w = one_warehouse(sent, "s_warehouse"), one_warehouse(doc, "t_warehouse")
	returning = bool(source_w) and source_w == target_w  # back where it was sent from: a resolution
	if returning:
		if not place_of(connection, source_w):
			raise NeedsMapping(
				_("Warehouse {0} is not on connection {1}'s Branch Map.").format(source_w, connection.name)
			)
		from_place = to_place = None
	else:
		from_place, to_place = places_for(connection, source_w, target_w, doc)
	parts = key_parts(sent.get("koomyo_id"))
	if parts and parts[0] == "send" and sent.get("koomyo_connection") == connection.name:
		transfer = client.get(f"/stock-transfers/{quote(parts[1], safe='')}")
	elif returning:
		raise Skipped(
			_(
				"{0} puts stock back where {1} sent it from, before anything of it was received. Koomyo never knew this transfer, so there is nothing to tell it."
			).format(doc.name, sent.name)
		)
	else:
		transfer = create_transfer(
			connection, client, sent, from_place, to_place, body_lines(connection, sent.items), event
		)
		mark(sent, connection, f"transfer:{transfer['id']}:send")
	if returning:
		return return_step(connection, client, doc, transfer, event)
	if (transfer.get("to") or {}).get("id") != to_place[1]:
		raise Fatal(
			_(
				"Koomyo transfer {0} goes to {1}, but {2} receives it at Warehouse {3}. A transfer has one destination: receive it there, or cancel the send and make it again."
			).format(
				transfer.get("number") or transfer["id"],
				(transfer.get("to") or {}).get("name") or "-",
				doc.name,
				target_w,
			)
		)
	lines = receive_rows(connection, transfer, doc.items, doc)
	answer = post_receive(connection, client, transfer, lines, doc, event)
	made = new_ids(transfer, answer, "receipts")
	if not made:
		raise Fatal(_("Koomyo did not write a delivery for {0}.").format(doc.name))
	mark(doc, connection, f"transfer:{transfer['id']}:recv:{made[-1]}")
	return ("Stock Entry", doc.name)


def return_step(connection, client, doc, transfer: dict, event):
	ids = line_ids(transfer)
	rows: dict[str, Decimal] = {}
	for row in doc.items:
		ingredient = linked_ingredient(row.item_code, connection)
		if ingredient not in ids:
			raise Fatal(
				_("{0}: {1} is not on Koomyo transfer {2}.").format(doc.name, row.item_code, transfer["id"])
			)
		rows[ids[ingredient]] = rows.get(ids[ingredient], Decimal(0)) + Decimal(str(flt(row.transfer_qty, 6)))
	body = {
		"resolutions": [
			{"line_id": line, "kind": "returned", "quantity": quantity_text(q, doc.name)}
			for line, q in rows.items()
		]
	}
	body["resolutions"][0]["note"] = _("ERPNext {0}").format(doc.name)[:200]
	try:
		answer = koomyo_write(
			connection,
			client,
			"POST",
			f"/stock-transfers/{quote(transfer['id'], safe='')}/resolve",
			body,
			idempotency_key(doc.doctype, doc.name, doc.modified, f"resolve-{event.get('attempt', 0)}"),
		)
	except KoomyoAPIError as e:
		explain_refusal(e, doc, transfer)
		raise
	made = new_ids(transfer, answer, "resolutions")
	if not made:
		raise Fatal(_("Koomyo did not write a resolution for {0}.").format(doc.name))
	mark(doc, connection, f"transfer:{transfer['id']}:res:{made[-1]}")
	return ("Stock Entry", doc.name)


# ------------------------------------------------------------------ cancel


@handles("erpnext.stock_transfer_cancel", switch=SWITCH, scopes=SCOPES)
def push_transfer_cancel(event: dict, connection, client: KoomyoClient | None = None):
	doc = frappe.get_doc("Stock Entry", event["data"]["name"])
	if doc.docstatus != 2 or not doc.get("koomyo_sent"):
		raise Skipped(_("{0} is not a cancelled entry that was sent to Koomyo.").format(doc.name))
	parts = key_parts(doc.get("koomyo_id"))
	if parts and parts[0] in ("recv", "res"):
		raise Skipped(
			_(
				"{0} was cancelled in ERPNext, but Koomyo has no call to undo a delivery or a resolution of a transfer. Correct that stock in Koomyo by hand."
			).format(doc.name)
		)
	transfer_id = parts[1] if parts else doc.koomyo_id
	client = client or KoomyoClient.for_connection(connection)
	try:
		answer = koomyo_write(
			connection,
			client,
			"POST",
			f"/stock-transfers/{quote(transfer_id, safe='')}/cancel",
			{"reason": _("Cancelled in ERPNext ({0})").format(doc.name)[:200]},
			idempotency_key(doc.doctype, doc.name, doc.modified, f"cancel-{event.get('attempt', 0)}"),
		)
	except KoomyoAPIError as e:
		if e.code == "transfer_partially_received":
			raise Fatal(
				_(
					"{0} is cancelled in ERPNext, but goods already arrived for its Koomyo transfer, so Koomyo cannot cancel it. Resolve what is left in Koomyo (return or write off), or make a transfer back."
				).format(doc.name)
			)
		if e.code == "transfer_locked":
			raise Skipped(_("Koomyo transfer of {0} is already cancelled or finished.").format(doc.name))
		raise
	if answer.get("object") != "stock_transfer":
		raise Fatal(_("Koomyo answered the cancel with something else."))
	return ("Stock Entry", doc.name)
