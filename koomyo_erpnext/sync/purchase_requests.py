"""Koomyo purchase requests -> ERPNext Material Requests.

Docs: docs.koomyo.com/guides/purchase-requests and /events/purchase-requests, OpenAPI schemas
PurchaseRequest, PurchaseRequestLine, Event_PurchaseRequest*, GET /purchase-requests (scope
purchasing.read; the restaurant needs the Inventory app and the purchase requests feature).

A branch asks for ingredients, purchasing approves, rejects or changes quantities in Koomyo (staff
only: the API has no call for it, so the decisions are read), and ERPNext orders. One purchase request
is one Material Request (type Purchase), keyed (connection, request id):

- it is made when purchasing has approved lines. A line is in the Material Request when its own status is
  approved, ordered, partly_received or received, at `approved_quantity` (what purchasing agreed to buy,
  never `quantity`, what the branch asked), delivered to the branch's Warehouse, needed by its date. A line
  still waiting for purchasing, rejected or cancelled is not in it. A request waiting for the branch manager
  (`awaiting_branch_approval`) has nothing approved yet, so nothing is made;
- each row carries the Koomyo request line id (`koomyo_line_id`), which the Purchase Order write-back needs;
- a change of what is approved rebuilds the Material Request (a submitted one cannot be edited) while
  nothing in ERPNext was ordered from it; after that the event fails with a message for a person, and
  nothing is forced. Progress (ordered, received) never rebuilds anything;
- a request rejected or cancelled in Koomyo cancels its Material Request, or fails the same way when ERPNext
  already ordered from it;
- the Material Request's name is written back with PUT /refs/purchase_request/{id} (scope refs.write), so
  GET /purchase-requests?system=erpnext&external_id=... finds it again.

One code path (`apply_request`) serves the five events and the periodic `updated_since` read (queued as
`koomyo.purchase_request_sync` Event Log rows), so a missed event is caught and a replay changes nothing.
"""

import hashlib
import json

import frappe
from frappe import _
from frappe.utils import escape_html, get_datetime, getdate

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import Fatal, Skipped
from koomyo_erpnext.sync.common import branch_row, find_existing, money, stamp, to_field
from koomyo_erpnext.sync.events import handles

SYNC_TYPE = "koomyo.purchase_request_sync"
EVENT_TYPES = (
	"purchase_request.submitted",
	"purchase_request.approved",
	"purchase_request.rejected",
	"purchase_request.cancelled",
	"purchase_request.updated",
	SYNC_TYPE,
)
SCOPES = ("purchasing.read", "refs.write")
STATUSES = ("submitted", "approved", "rejected", "ordered", "partly_received", "received", "cancelled")
ORDERABLE = ("approved", "ordered", "partly_received", "received")


@handles(*EVENT_TYPES, switch="sync_purchase_requests", scopes=SCOPES)
def on_purchase_request(event: dict, connection):
	card = event["data"]
	if card.get("object") != "purchase_request" or not card.get("id"):
		raise Fatal(_("Koomyo sent something that is not a purchase request."))
	return apply_request(card, connection)


def apply_request(card: dict, connection) -> tuple[str, str]:
	status = card.get("status")
	if status not in STATUSES:
		raise Fatal(
			_("Koomyo sent purchase request {0} with status {1!r}.").format(card.get("number"), status)
		)
	number = card.get("number") or card["id"]
	mr = existing_request(card, connection)
	if mr and is_stale(mr, card):
		return ("Material Request", mr.name)

	if status in ("rejected", "cancelled"):
		return close_request(mr, card, connection)

	wanted = included_lines(card)
	if not wanted:
		if mr and mr.docstatus == 1:
			return rebuild_or_flag(mr, card, connection, wanted)
		raise Skipped(
			_(
				"Purchase request {0} has nothing approved yet: ERPNext is told when purchasing approves lines."
			).format(number)
		)

	if not mr:
		return ("Material Request", create_request(card, connection, wanted).name)
	if mr.docstatus == 2:
		raise Skipped(
			_("Material Request {0} was cancelled in ERPNext; it is left as it is.").format(mr.name)
		)
	if mr.get("koomyo_content") == fingerprint(card, wanted):
		note_seen(mr, card)
		return ("Material Request", mr.name)
	return rebuild_or_flag(mr, card, connection, wanted)


def existing_request(card: dict, connection):
	name = find_existing("Material Request", connection, card["id"])
	ref = (card.get("external_refs") or {}).get(connection.ref_system or "erpnext")
	if not name and ref and frappe.db.exists("Material Request", ref):
		name = ref
	if not name:
		return None
	mr = frappe.get_doc("Material Request", name)
	if mr.company != connection.company:
		raise Fatal(
			_("Material Request {0} belongs to {1}, not to {2}.").format(
				mr.name, mr.company, connection.company
			)
		)
	return mr


def is_stale(mr, card: dict) -> bool:
	seen, new = mr.get("koomyo_updated_at"), card.get("updated_at")
	return bool(seen and new and get_datetime(new) < get_datetime(seen))


def note_seen(mr, card: dict):
	if card.get("updated_at"):
		mr.db_set("koomyo_updated_at", card["updated_at"], update_modified=False)


def included_lines(card: dict) -> list[dict]:
	"""Lines purchasing approved, at the quantity it agreed to (never the quantity the branch asked for)."""
	lines = []
	for line in card.get("lines") or []:
		if line.get("status") not in ORDERABLE:
			continue
		quantity = line.get("approved_quantity")
		if quantity is None:
			continue
		qty = money(quantity, "approved_quantity")
		if qty > 0:
			lines.append({"line": line, "qty": qty})
	return lines


def fingerprint(card: dict, wanted: list[dict]) -> str:
	material = {
		"branch": card.get("branch_id"),
		"needed_by": card.get("needed_by"),
		"lines": [
			[
				w["line"].get("id"),
				(w["line"].get("ingredient") or {}).get("id"),
				str(w["qty"]),
				w["line"].get("needed_by"),
			]
			for w in wanted
		],
	}
	return hashlib.sha1(json.dumps(material, sort_keys=True).encode()).hexdigest()


def create_request(card: dict, connection, wanted: list[dict]):
	from koomyo_erpnext.sync.customers import write_reference
	from koomyo_erpnext.sync.stock import ingredient_item

	branch = branch_row(connection, card.get("branch_id"))
	if not branch.warehouse:
		raise Fatal(_("The branch of purchase request {0} has no Warehouse.").format(card.get("number")))
	client = KoomyoClient.for_connection(connection)
	date = str(getdate(card.get("business_date") or card.get("created_at")))
	needed = str(getdate(card["needed_by"])) if card.get("needed_by") else date
	if needed < date:
		needed = date
	mr = frappe.new_doc("Material Request")
	mr.update(
		{
			"material_request_type": "Purchase",
			"company": connection.company,
			"transaction_date": date,
			"schedule_date": needed,
			"set_warehouse": branch.warehouse,
		}
	)
	text = _("Koomyo purchase request {0} of {1}.").format(
		card.get("number") or card["id"], escape_html(card.get("branch_name") or "")
	)
	if card.get("notes"):
		text += " " + escape_html(card["notes"])[:400]
	stamp(mr, connection, card["id"], card.get("branch_id"))
	for w in wanted:
		line = w["line"]
		ingredient = line.get("ingredient") or {}
		item = ingredient_item(
			connection,
			{
				"ingredient_id": ingredient.get("id"),
				"ingredient": ingredient.get("name"),
				"unit": ingredient.get("unit"),
			},
			client=client,
		)
		line_needed = str(getdate(line["needed_by"])) if line.get("needed_by") else needed
		row = {
			"item_code": item,
			"qty": to_field(w["qty"], 6),
			"warehouse": branch.warehouse,
			"schedule_date": max(line_needed, date),
			"koomyo_line_id": line["id"],
		}
		if line.get("note"):
			row["description"] = escape_html(line["note"])[:300]
		mr.append("items", row)
	mr.insert(ignore_permissions=True)
	mr.add_comment("Comment", text)
	mr.submit()
	mr.db_set(
		{"koomyo_updated_at": card.get("updated_at"), "koomyo_content": fingerprint(card, wanted)},
		update_modified=False,
	)
	write_reference(connection, client, "purchase_request", card, mr)
	return mr


def documents_against(mr) -> list[str]:
	"""What ERPNext already made from this Material Request (an order, a transfer)."""
	found = []
	for doctype, child in (
		("Purchase Order", "Purchase Order Item"),
		("Stock Entry", "Stock Entry Detail"),
		("Request for Quotation", "Request for Quotation Item"),
		("Supplier Quotation", "Supplier Quotation Item"),
	):
		parents = frappe.get_all(
			child, filters={"material_request": mr.name, "docstatus": ("<", 2)}, pluck="parent", distinct=True
		)
		found += [f"{doctype} {p}" for p in parents]
	return found


def rebuild_or_flag(mr, card: dict, connection, wanted: list[dict]):
	"""What purchasing approved changed: make the Material Request again, if nothing was ordered from it."""
	against = documents_against(mr)
	if against:
		note_seen(mr, card)
		raise Fatal(
			_(
				"Koomyo changed what was approved on purchase request {0}, but ERPNext already ordered from Material Request {1} ({2}). Nothing was changed: adjust by hand, then press Retry."
			).format(card.get("number") or card["id"], mr.name, ", ".join(against[:5]))
		)
	old = mr.name
	mr.flags.ignore_permissions = True  # the app cancels what it made, whoever queued the job
	mr.cancel()
	mr.db_set({"koomyo_connection": None, "koomyo_id": None}, update_modified=False)
	if not wanted:
		mr.add_comment("Comment", _("Cancelled: nothing of the Koomyo request is approved any more."))
		return ("Material Request", mr.name)
	new = create_request(card, connection, wanted)
	new.add_comment(
		"Comment",
		_("Replaces {0}, which was cancelled because Koomyo changed what was approved.").format(old),
	)
	return ("Material Request", new.name)


def close_request(mr, card: dict, connection):
	number = card.get("number") or card["id"]
	if not mr:
		raise Skipped(
			_("Purchase request {0} was {1} in Koomyo; there is nothing in ERPNext to cancel.").format(
				number, card.get("status")
			)
		)
	if mr.docstatus == 2:
		return ("Material Request", mr.name)
	against = documents_against(mr)
	if against:
		raise Fatal(
			_(
				"Koomyo {0} purchase request {1}, but ERPNext already ordered from Material Request {2} ({3}). Nothing was cancelled: decide by hand, then press Retry."
			).format(card.get("status"), number, mr.name, ", ".join(against[:5]))
		)
	mr.flags.ignore_permissions = True
	mr.cancel()
	reason = card.get("cancel_reason") or card.get("reject_reason") or "-"
	mr.add_comment(
		"Comment",
		_("{0} in Koomyo. Reason: {1}").format(card.get("status").capitalize(), escape_html(reason)),
	)
	return ("Material Request", mr.name)


# ------------------------------------------------------------------ the safety net


def sync_purchase_requests(connection_name: str, client: KoomyoClient | None = None) -> dict:
	"""Read GET /purchase-requests?updated_since= and queue each changed request through the same path.

	A restaurant without the Inventory app or the purchase requests feature answers 403
	(feature_not_available or app_not_enabled): nothing to read, said plainly in the summary.
	"""
	from koomyo_erpnext.exceptions import KoomyoAPIError
	from koomyo_erpnext.sync.events import enqueue_processing, store_event

	connection = frappe.get_doc("Koomyo Connection", connection_name)
	summary = {"read": 0, "queued": 0}
	if not (connection.enabled and connection.sync_purchase_requests and connection.tenant_id):
		return summary
	if connection.status == "Needs Re-sync":
		return summary
	since = connection.purchase_request_cursor or f"{connection.start_from_date}T00:00:00Z"
	client = client or KoomyoClient.for_connection(connection)
	newest = connection.purchase_request_cursor
	try:
		for card in client.iter_list("/purchase-requests", {"updated_since": since}):
			summary["read"] += 1
			if card.get("object") != "purchase_request" or not card.get("id") or not card.get("updated_at"):
				continue
			event = {
				"id": f"pr-sync:{card['id']}:{card['updated_at']}",
				"type": SYNC_TYPE,
				"seq": 0,
				"tenant_id": connection.tenant_id,
				"created": card["updated_at"],
				"business_date": card.get("business_date"),
				"branch_id": card.get("branch_id"),
				"entity": {"type": "purchase_request", "id": card["id"]},
				"data": card,
			}
			_name, created = store_event(connection, event, "Feed")
			summary["queued"] += 1 if created else 0
			if not newest or get_datetime(card["updated_at"]) > get_datetime(newest):
				newest = card["updated_at"]
	except KoomyoAPIError as e:
		if e.status == 403 and e.code in ("feature_not_available", "app_not_enabled"):
			summary["error"] = feature_message(e)
			return summary
		raise
	if newest and newest != connection.purchase_request_cursor:
		connection.db_set("purchase_request_cursor", newest, update_modified=False)
	frappe.db.commit()
	if summary["queued"]:
		enqueue_processing(connection_name)
	return summary


def feature_message(error) -> str:
	return _(
		"This restaurant cannot use purchase requests ({0}): it needs Koomyo's Inventory app and the purchase requests feature. Ask the restaurant to switch them on, or switch Sync Purchase Requests off."
	).format(error.code)
