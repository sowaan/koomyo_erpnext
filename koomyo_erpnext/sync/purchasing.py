"""Purchasing between ERPNext and Koomyo.

Docs: docs.koomyo.com/guides/stock-purchasing, /guides/customers-suppliers, /events/purchasing,
OpenAPI POST /suppliers, POST /purchase-orders, POST /purchase-orders/{id}/receive,
PATCH /ingredients/{id}, schemas SupplierInput, PurchaseOrderInput, ReceiveInput, PurchaseOrder.

ERPNext -> Koomyo (queued from doc_events, processed like any event, visible in the Event Log):
- Purchase Order submitted for a Warehouse on a connection's Branch Map
  -> POST /suppliers (upsert by external_ref) then POST /purchase-orders (external_ref = PO name).
- Purchase Receipt submitted against such an order -> POST /purchase-orders/{id}/receive.
- Supplier changed (once linked) -> POST /suppliers upsert.
- Item valuation rate changed (a linked ingredient) -> PATCH /ingredients/{id}.
Every write carries an Idempotency-Key from the document name and its modified time.

Koomyo -> ERPNext: purchase orders (created, updated, cancelled, received) are in
sync/purchase_orders.py; suppliers made from them are created here (`koomyo_supplier`).
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import add_to_date, flt, get_datetime, now_datetime

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import Fatal, KoomyoAPIError, NeedsMapping, Skipped
from koomyo_erpnext.sync.common import find_existing, money, posting_date, stamp
from koomyo_erpnext.sync.events import handles, idempotency_key, koomyo_write, store_outbound
from koomyo_erpnext.sync.suppliers import KOOMYO_ORIGIN

WRITE_SCOPES = ("purchasing.read", "purchasing.write", "suppliers.write", "ingredients.write")
QTY = Decimal("0.0001")


def text(value) -> str:
	"""A Frappe number as Koomyo decimal text, 4 places at most, no float artefacts."""
	d = Decimal(str(flt(value, 6))).quantize(QTY)
	return format(d.normalize(), "f") if d != d.to_integral() else format(d.quantize(Decimal(1)), "f")


# ------------------------------------------------------------------ doc_events (in the user's request)


def connections_for_company(company: str) -> list:
	names = frappe.get_all(
		"Koomyo Connection", filters={"company": company, "enabled": 1, "sync_purchasing": 1}, pluck="name"
	)
	return [frappe.get_cached_doc("Koomyo Connection", n) for n in names]


def connection_for_warehouse(company: str, warehouse: str | None):
	"""The connection (and its map row) whose Branch Map holds this Warehouse."""
	if not warehouse:
		return None, None
	for connection in connections_for_company(company):
		for row in connection.branch_map:
			if row.warehouse == warehouse:
				return connection, row
	return None, None


def on_purchase_order_submit(doc, method=None):
	if doc.get("koomyo_connection"):
		return  # made from a Koomyo order: Koomyo already has it
	connection, _row = connection_for_warehouse(doc.company, po_warehouse(doc))
	if not connection and not doc.get("set_warehouse"):
		# Rows in several Warehouses (an order split by place): the connection that holds any of them.
		for warehouse in sorted({row.warehouse for row in doc.items if row.warehouse}):
			connection, _row = connection_for_warehouse(doc.company, warehouse)
			if connection:
				break
	if not connection:
		# An order made from Koomyo purchase requests goes to several branches' Warehouses: its connection is the
		# one the Material Requests came from.
		connection = request_connection(doc)
	if connection:
		store_outbound(connection, "erpnext.purchase_order", doc)
		return
	# Not sent, and the person should know why (this is not an error: a company may order for places Koomyo does not run).
	candidates = connections_for_company(doc.company)
	if candidates:
		warehouse = po_warehouse(doc) or _("(no single Warehouse)")
		note = _(
			"Not sent to Koomyo: Warehouse {0} is not on the Branch Map of connection {1}. If this order should go to Koomyo, add the Warehouse to the Branch Map and send it again (cancel and amend, or save it with a change)."
		).format(warehouse, ", ".join(c.name for c in candidates))
		doc.add_comment("Comment", note)
		frappe.msgprint(note, indicator="orange", alert=True)


def before_purchase_order_submit(doc, method=None):
	"""A draft kept from Koomyo becomes a real order only when it is ordered in Koomyo (then this app submits it)."""
	if doc.get("koomyo_content") and doc.get("koomyo_id") and not doc.flags.get("from_koomyo"):
		order = frappe.db.get_value(
			"Purchase Order", doc.name, "koomyo_updated_at"
		)  # kept so the message can say when it was last seen
		frappe.throw(
			_(
				"{0} is a draft in Koomyo (last seen {1}). It becomes a real order when it is ordered in Koomyo: this app then submits it. Do not submit it by hand."
			).format(doc.name, order or "-")
		)


def before_material_request_submit(doc, method=None):
	"""A draft kept from a Koomyo request becomes real only when purchasing approves it in Koomyo."""
	if doc.get("koomyo_content") and doc.get("koomyo_id") and not doc.flags.get("from_koomyo"):
		frappe.throw(
			_(
				"{0} is a draft in Koomyo: nothing is approved there yet. It becomes a real request when purchasing approves it in Koomyo: this app then submits it. Do not submit it by hand."
			).format(doc.name)
		)


def on_purchase_order_cancel(doc, method=None):
	"""A Purchase Order sent to Koomyo was cancelled in ERPNext: Koomyo is cleaned up (cancel, reverse, link removed)."""
	if not doc.get("koomyo_connection") or not doc.get("koomyo_id") or doc.get("koomyo_content"):
		return  # never sent to Koomyo, or an order Koomyo made itself (its own cancel comes as an event)
	connection = frappe.get_cached_doc("Koomyo Connection", doc.koomyo_connection)
	if connection.enabled and connection.sync_purchasing:
		store_outbound(connection, "erpnext.purchase_order_cancel", doc, extra={"koomyo_id": doc.koomyo_id})


def on_purchase_receipt_submit(doc, method=None):
	if doc.get("is_return"):
		return  # goods sent back to the supplier: sync/purchase_edits.py
	if doc.get("koomyo_connection"):
		return  # made from a Koomyo receipt: Koomyo already has it
	orders = {row.purchase_order for row in doc.items if row.purchase_order}
	for order in orders:
		linked = frappe.db.get_value(
			"Purchase Order", order, ["koomyo_connection", "koomyo_id"], as_dict=True
		)
		if linked and linked.koomyo_connection:
			connection = frappe.get_cached_doc("Koomyo Connection", linked.koomyo_connection)
			if connection.enabled and connection.sync_purchasing:
				store_outbound(
					connection,
					"erpnext.purchase_receipt",
					doc,
					action=f"receive-{order}",
					extra={"purchase_order": order},
				)


def queue_closed_orders(connection_name: str) -> int:
	"""Orders a person closed in ERPNext (the Close button fires no document event, so they are looked for).

	Each Closed Purchase Order sent to Koomyo is queued once; the handler tells Koomyo to finish it short
	only if part of it arrived. Called every few minutes with the catch-up.
	"""
	connection = frappe.get_doc("Koomyo Connection", connection_name)
	if not (connection.enabled and connection.sync_purchasing):
		return 0
	queued = 0
	for name in frappe.get_all(
		"Purchase Order",
		filters={
			"company": connection.company,
			"koomyo_connection": connection.name,
			"koomyo_id": ("is", "set"),
			"koomyo_content": ("is", "not set"),
			"status": "Closed",
			"docstatus": 1,
			"modified": (">", add_to_date(now_datetime(), days=-30)),
		},
		pluck="name",
	):
		if store_outbound(connection, "erpnext.purchase_order_close", frappe.get_doc("Purchase Order", name)):
			queued += 1
	return queued


def on_supplier_update(doc, method=None):
	if doc.get("koomyo_connection") and doc.get("koomyo_branch") != KOOMYO_ORIGIN:
		connection = frappe.get_cached_doc("Koomyo Connection", doc.koomyo_connection)
		if connection.enabled and connection.sync_purchasing:
			store_outbound(connection, "erpnext.supplier", doc)


def on_item_update(doc, method=None):
	if not doc.get("koomyo_connection") or not doc.is_stock_item:
		return
	# Only a person's edit of an existing Item: a new Item (or one made by the sync) has no cost to send.
	if doc.get_doc_before_save() is None or not doc.has_value_changed("valuation_rate"):
		return
	connection = frappe.get_cached_doc("Koomyo Connection", doc.koomyo_connection)
	if connection.enabled and connection.sync_purchasing:
		store_outbound(connection, "erpnext.item_cost", doc)


def request_connection(po):
	"""The enabled connection whose purchase requests (Material Requests) this Purchase Order is made from."""
	names = {
		frappe.db.get_value("Material Request", row.material_request, "koomyo_connection")
		for row in po.items
		if row.get("material_request")
	} - {None}
	if len(names) != 1:
		return None
	connection = frappe.get_cached_doc("Koomyo Connection", names.pop())
	if connection.enabled and connection.sync_purchasing and connection.sync_purchase_requests:
		return connection
	return None


def request_line_of(item, connection) -> str | None:
	"""The Koomyo purchase request line a Purchase Order row fills, from its Material Request row."""
	if not item.get("material_request_item"):
		return None
	row = frappe.db.get_value(
		"Material Request Item", item.material_request_item, ["koomyo_line_id", "parent"], as_dict=True
	)
	if not row or not row.koomyo_line_id:
		return None
	if frappe.db.get_value("Material Request", row.parent, "koomyo_connection") != connection.name:
		return None
	return row.koomyo_line_id


def po_warehouse(doc) -> str | None:
	warehouses = {row.warehouse for row in doc.items if row.warehouse} or (
		{doc.set_warehouse} if doc.set_warehouse else set()
	)
	return warehouses.pop() if len(warehouses) == 1 else (doc.set_warehouse or None)


# ------------------------------------------------------------------ outbound handlers


@handles("erpnext.purchase_order", switch="sync_purchasing", scopes=WRITE_SCOPES)
def push_purchase_order(event: dict, connection, client: KoomyoClient | None = None):
	po = frappe.get_doc("Purchase Order", event["data"]["name"])
	if po.docstatus != 1:
		raise Skipped(_("Purchase Order {0} is not submitted any more.").format(po.name))
	if po.koomyo_id:
		# Sent before: only make sure it is not left a draft (a Retry after a failed mark).
		ensure_ordered(connection, client or KoomyoClient.for_connection(connection), po)
		return ("Purchase Order", po.name)
	shares = order_shares(po, connection)
	with_shares = any(shares.values())
	split = with_shares and all(v == SPLIT for v in shares.values())
	row = None
	if split:
		# One Koomyo line per ingredient with a share per warehouse: every warehouse must be a place Koomyo knows.
		for warehouse in sorted({item.warehouse for item in po.items}):
			destination_of(connection, warehouse)
	elif with_shares:
		missing = [item.item_code for item in po.items if not shares[item.name]]
		if missing:
			raise Fatal(
				_(
					"Purchase Order {0} fills Koomyo purchase requests, but row(s) {1} come from no Koomyo Material Request. Koomyo needs every row of such an order to fill a request line: split the order."
				).format(po.name, ", ".join(sorted(set(missing))))
			)
	else:
		warehouses = {row.warehouse for row in po.items}
		if len(warehouses) != 1:
			raise Fatal(
				_(
					"Purchase Order {0} goes to {1} warehouses. Koomyo takes one place per order: split it."
				).format(po.name, len(warehouses))
			)
		_connection, row = connection_for_warehouse(po.company, warehouses.pop())
		if not row:
			raise NeedsMapping(
				_("The Warehouse of Purchase Order {0} is not on connection {1}'s Branch Map.").format(
					po.name, connection.name
				)
			)

	client = client or KoomyoClient.for_connection(connection)
	supplier_id = push_supplier_doc(frappe.get_doc("Supplier", po.supplier), connection, client)

	lines = order_lines_for_koomyo(po, connection, shares)
	tax, residual = tax_fields(po, connection)
	body = {
		"supplier_id": supplier_id,
		"notes": order_notes(po, residual),
		"lines": lines,
		**tax,
		"external_ref": {"system": connection.ref_system or "erpnext", "external_id": po.name},
	}
	if row is not None and row.location_type != "Warehouse":
		body["branch_id"] = row.koomyo_branch_id  # leave out to receive into the main warehouse (docs)
	# The key says which document version and which body: the same key with another body is refused (422).
	body_hash = hashlib.sha1(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:10]
	try:
		answer = koomyo_write(
			connection,
			client,
			"POST",
			"/purchase-orders",
			body,
			# A retry after a refusal gets its own key: Koomyo may answer the same key with the first (refused)
			# answer again. The external_ref still makes the order unique for good.
			idempotency_key(
				"Purchase Order",
				po.name,
				po.modified,
				f"po-{body_hash}" if event.get("attempt", 1) <= 1 else f"po-{body_hash}-{event['attempt']}",
			),
		)
	except KoomyoAPIError as e:
		explain_order_refusal(e, po)
		raise
	if answer.get("object") != "purchase_order" or not answer.get("id"):
		raise Fatal(_("Koomyo answered the purchase order with something else."))
	check_same_document(po, answer)  # before anything is linked: a reused number is not this document
	po.db_set(
		{
			"koomyo_connection": connection.name,
			"koomyo_id": answer["id"],
			"koomyo_branch": body.get("branch_id"),
		}
	)
	compare_totals(po, answer)
	ensure_ordered(connection, client, po, answer)
	return ("Purchase Order", po.name)


def created_at_in_system_time(order: dict):
	"""Koomyo's `created_at` (UTC) as the naive datetime ERPNext stores in `creation`."""
	text = order.get("created_at")
	if not text:
		return None
	moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
	if moment.tzinfo is None:
		moment = moment.replace(tzinfo=timezone.utc)
	return moment.astimezone(ZoneInfo(frappe.utils.get_system_timezone())).replace(tzinfo=None)


def older_than_document(order: dict, creation) -> bool:
	"""True when Koomyo's order was made before the ERPNext document existed: it cannot be this document's order.

	ERPNext reuses a Purchase Order number after a delete, and a Koomyo order may still be linked to that number.
	A genuine order is made after the document (minute of slack for the two clocks).
	"""
	made = created_at_in_system_time(order)
	return bool(made and creation and made < get_datetime(creation) - timedelta(seconds=60))


def check_same_document(po, order: dict):
	if older_than_document(order, po.creation):
		raise Fatal(
			_(
				"Koomyo answered with order {0}, which was made on {1}, before Purchase Order {2} was created ({3}). ERPNext reused the number {2} after a delete, and that Koomyo order is still linked to it. Nothing was linked or sent. Clear the old order in Koomyo (cancel it and remove its link), then press Retry."
			).format(order.get("number") or order.get("id"), order.get("created_at"), po.name, po.creation)
		)


def ensure_ordered(connection, client, po, order: dict | None = None):
	"""POST /purchase-orders/{id}/mark-ordered: a draft becomes ordered, without emailing the supplier.

	ERPNext placed the order itself, and a draft in Koomyo is a plan staff may edit or send (Send would email
	the supplier again). 409 not_a_draft is fine (it is ordered or further on); 409 stale_version re-reads the
	order once; 409 delivery_places_incomplete means the shares do not add up or have no place.
	"""
	if not connection.get("mark_orders_ordered"):
		return
	path = f"/purchase-orders/{quote(po.koomyo_id, safe='')}"
	for attempt in (1, 2):
		order = order or client.get(path)
		if order.get("status") != "draft":
			return
		body = {"updated_at": order["updated_at"]} if order.get("updated_at") else {}
		if po.get("schedule_date"):
			body["expected_date"] = str(po.schedule_date)
		try:
			koomyo_write(
				connection,
				client,
				"POST",
				path + "/mark-ordered",
				body,
				idempotency_key(
					"Purchase Order", po.name, po.modified, f"mark-ordered-{order.get('updated_at')}"
				),
			)
			return
		except KoomyoAPIError as e:
			if e.code == "not_a_draft":
				return
			if e.code == "stale_version" and attempt == 1:
				order = None  # changed meanwhile: read it again and repeat on that
				continue
			if e.code == "delivery_places_incomplete":
				raise Fatal(
					_(
						"Koomyo purchase order {0} (from {1}) was created but cannot be marked ordered: the shares of its lines do not add up or a share has no place. Fix it in Koomyo, then press Retry."
					).format(po.koomyo_id, po.name)
				)
			raise


def compare_totals(po, answer: dict):
	"""Koomyo works out the totals with its own rules: its figure is read, never recomputed, and a difference
	from ERPNext's own total is said (a few cents are rounding)."""
	if answer.get("total_ordered") is None:
		return
	koomyo = money(answer["total_ordered"], "total_ordered")
	ours = Decimal(str(flt(po.grand_total, 2)))
	note = _("Koomyo's total for this order is {0} (ERPNext's is {1}).").format(koomyo, ours)
	po.add_comment("Comment", note)
	if abs(koomyo - ours) >= Decimal("1"):
		from koomyo_erpnext.sync.events import warn

		warn(
			_(
				"Koomyo's total for {0} is {1}, ERPNext's is {2}: a difference of more than rounding. Check its tax and charges in Koomyo before sending it."
			).format(po.name, koomyo, ours)
		)


REFUSALS = {
	"request_line_not_orderable": _(
		"Koomyo will not take Purchase Order {0}: a purchase request line it fills is not approved (or is not orderable). Nothing was created in Koomyo. Wait until purchasing approves it, then press Retry."
	),
	"request_quantity_exceeded": _(
		"Koomyo will not take Purchase Order {0}: it orders more than purchasing approved for a request line, less what other orders already hold (if Koomyo's own staff also order, whoever orders a line first wins: ask them to set Purchase requests to the external system). Nothing was created in Koomyo."
	),
	"feature_not_available": _(
		"Koomyo will not take Purchase Order {0} with request shares: the restaurant needs Koomyo's purchase requests feature. Nothing was created in Koomyo."
	),
}


def explain_order_refusal(error, po):
	"""A refusal creates nothing in Koomyo; say what to do (never retried by itself)."""
	message = REFUSALS.get(error.code)
	if message and "partly received" in (error.message or "").lower():
		raise Fatal(
			_(
				"Koomyo will not take Purchase Order {0}: its purchase request is already partly received (for example by a transfer from a warehouse), and Koomyo does not take an order on such a request, even for lines that are still approved. Nothing was created in Koomyo. This is Koomyo's rule: the order stays in ERPNext only. Koomyo said: {1}"
			).format(po.name, error.message)
		)
	if message:
		raise Fatal(message.format(po.name) + " (" + error.message + ")")
	if error.code == "invalid_parameter" and any(row.get("material_request_item") for row in po.items):
		raise Fatal(
			_(
				"Koomyo refused Purchase Order {0}: {1}. Check that its quantities match the Material Request rows (a share must be for the request line's own ingredient and branch)."
			).format(po.name, error.message)
		)


def order_notes(po, residual: list[str] | None = None) -> str:
	"""The order's notes for Koomyo, with whatever of the ERPNext taxes and charges could not be sent as figures."""
	base = po.get("remarks") or _("ERPNext {0}").format(po.name)
	if not residual:
		return base[:500]
	from koomyo_erpnext.sync.events import warn

	warn(
		_(
			"Not everything in the taxes and charges of {0} could be sent as figures ({1}); it is in the order's notes: enter it on the order in Koomyo before sending it."
		).format(po.name, "; ".join(residual))
	)
	return (_("ERPNext, not sent as figures: {0}.").format("; ".join(residual)) + " " + base)[:500]


def tax_fields(po, connection) -> tuple[dict, list[str]]:
	"""The order's tax and charges for POST /purchase-orders, from its Purchase Taxes and Charges.

	Koomyo calculates the totals itself (docs: POST /purchase-orders), so only the inputs go: one order tax (name,
	rate, exclusive or inclusive, recoverable or not) and the additional charges and withholding. A row is a
	withholding when its account is the Withholding Tax Account, a tax when it is the Purchase Tax Account or an
	account of type Tax, else a charge. A charge on the Landed Cost Account is spread over cost as the connection's
	Charges in Landed Cost says. What cannot be said (a second tax, a document discount, a rate that cannot be
	worked out) comes back as `residual` text for the notes, never guessed.
	"""
	fields: dict = {}
	charges: list[dict] = []
	residual: list[str] = []
	net_total = sum((Decimal(str(flt(i.base_net_amount, 6))) for i in po.items), Decimal(0))
	if flt(po.get("discount_amount")) or flt(po.get("additional_discount_percentage")):
		residual.append(_("a discount on the whole order"))
	for row in po.get("taxes") or []:
		amount = Decimal(str(flt(row.tax_amount, 6)))
		if not amount:
			continue
		label = (row.description or row.account_head or _("Charge"))[:60]
		account = row.account_head
		account_type = frappe.db.get_value("Account", account, "account_type")
		deduct = row.get("add_deduct_tax") == "Deduct"
		if account and account == connection.get("purchase_withholding_account"):
			charges.append(
				{
					"label": label,
					"kind": "amount",
					"value": text(abs(amount)),
					"direction": "deduct",
					"role": "withholding",
					"taken_of": "total",
				}
			)
		elif not deduct and (account == connection.get("purchase_tax_account") or account_type == "Tax"):
			if "tax_rate" in fields:
				residual.append(f"{label} {text(amount)}")
				continue
			if row.charge_type == "On Net Total" and flt(row.rate):
				rate = Decimal(str(flt(row.rate, 6)))
			elif net_total:
				rate = (abs(amount) * 100 / net_total).quantize(Decimal("0.0001"))
			else:
				residual.append(f"{label} {text(amount)}")
				continue
			fields.update(
				{
					"tax_name": label[:60],
					"tax_rate": text(rate),
					"tax_mode": "inclusive" if row.get("included_in_print_rate") else "exclusive",
					"tax_recoverable": row.get("category") == "Total",
				}
			)
		else:
			spread = "none"
			if account and account == connection.get("purchase_charge_account"):
				spread = {"None": "none", "By Quantity": "quantity"}.get(
					connection.get("landed_cost_method"), "value"
				)
			elif row.get("category") in ("Valuation and Total", "Valuation"):
				spread = "value"
			charges.append(
				{
					"label": label,
					"kind": "amount",
					"value": text(abs(amount)),
					"direction": "deduct" if deduct else "add",
					"spread_over_cost": spread,
					"role": "charge",
				}
			)
	if len(charges) > 20:
		residual += [f"{c['label']} {c['value']}" for c in charges[20:]]
		charges = charges[:20]
	if charges:
		fields["charges"] = charges
	return fields, residual


def share_destination(po, item, connection) -> tuple[str, str]:
	"""Where a share is delivered: the place of the Purchase Order row's Warehouse on the Branch Map.

	A branch's Warehouse means straight to that branch (which must be the request's own branch: Koomyo
	delivers a branch share to its own branch), a Koomyo warehouse's means to that warehouse. This is what
	the Purchase Receipt of the row is received into, so ERPNext and Koomyo agree where the goods go.
	"""
	kind, place_id = destination_of(connection, item.warehouse)
	if kind == "branch" and item.get("material_request_item"):
		parent = frappe.db.get_value("Material Request Item", item.material_request_item, "parent")
		wanted = frappe.db.get_value("Material Request", parent, "koomyo_branch")
		if wanted and wanted != place_id:
			raise NeedsMapping(
				_(
					"Purchase Order {0}: Item {1} is for another branch than Warehouse {2} belongs to. Koomyo delivers a branch's share to that branch itself: use the Warehouse of the request's own branch, or a central Koomyo warehouse's Warehouse, on that row."
				).format(po.name, item.item_code, item.warehouse)
			)
	return kind, place_id


SPLIT = "split"  # a share that fills no request line: a row of an order split over several warehouses


def share_entry(request_line: str, kind: str, place_id: str, qty: Decimal) -> dict:
	entry = {"quantity": text(qty), "destination_type": kind, "destination_id": place_id}
	if request_line != SPLIT:
		entry["request_line_id"] = request_line
	elif kind == "branch":
		entry["branch_id"] = place_id  # a branch share is for its own branch
	return entry


def order_shares(po, connection) -> dict:
	"""Which rows are shares. Rows made from Koomyo request lines fill them; an order with no such row but rows in
	several warehouses is split by warehouse (one share per row), so Koomyo can take each delivery at its place."""
	shares = {item.name: request_line_of(item, connection) for item in po.items}
	if not any(shares.values()) and len({item.warehouse for item in po.items}) > 1:
		return {item.name: SPLIT for item in po.items}
	return shares


def order_lines_for_koomyo(po, connection, shares: dict) -> list[dict]:
	"""One Koomyo line per ingredient. A row that fills a purchase request line becomes a share of its line.

	Koomyo checks that a line's shares add up to its quantity, so the rows of one Item (made from several
	branches' Material Requests) are summed and each request line keeps its own share. One price per
	ingredient: rows of one Item with different rates cannot be one line.
	"""
	grouped: dict[str, dict] = {}
	for item in po.items:
		ingredient_id = linked_ingredient(item.item_code, connection)
		factor = Decimal(str(flt(item.conversion_factor, 9) or 1))
		discount = Decimal(str(flt(item.get("discount_percentage"), 6)))
		inclusive = any(r.get("included_in_print_rate") for r in po.get("taxes") or [])
		if discount:  # Koomyo takes the price before the line discount and the percent
			price = item.get("base_price_list_rate") or item.base_rate
		else:
			price = item.base_rate if inclusive else item.base_net_rate
		cost = Decimal(str(flt(price, 6))) / factor
		qty = Decimal(str(flt(item.stock_qty, 6)))
		share = shares.get(item.name)
		line = grouped.get(ingredient_id)
		if line is None:
			grouped[ingredient_id] = line = {
				"qty": Decimal(0),
				"cost": cost,
				"discount": discount,
				"shares": {},
			}
		elif not share:
			raise Fatal(
				_("Purchase Order {0} has Item {1} twice. Koomyo needs one line per ingredient.").format(
					po.name, item.item_code
				)
			)
		elif (line["cost"], line["discount"]) != (cost, discount):
			raise Fatal(
				_(
					"Purchase Order {0}: the rows of Item {1} have different rates ({2} and {3}). Koomyo takes one price per ingredient: give them one rate."
				).format(po.name, item.item_code, text(line["cost"]), text(cost))
			)
		line["qty"] += qty
		if share:
			place = share_destination(po, item, connection)
			key = (share, *place)
			line["shares"][key] = line["shares"].get(key, Decimal(0)) + qty
	out = []
	for ingredient_id, line in grouped.items():
		entry = {
			"ingredient_id": ingredient_id,
			"quantity_ordered": text(line["qty"]),
			"unit_cost": text(line["cost"]),
		}
		if line["discount"]:
			entry["discount_percent"] = text(line["discount"])
		if line["shares"]:
			entry["allocations"] = [
				share_entry(request_line, kind, place_id, qty)
				for (request_line, kind, place_id), qty in line["shares"].items()
			]
		out.append(entry)
	return out


@handles("erpnext.purchase_receipt", switch="sync_purchasing", scopes=WRITE_SCOPES)
def push_purchase_receipt(event: dict, connection, client: KoomyoClient | None = None):
	receipt = frappe.get_doc("Purchase Receipt", event["data"]["name"])
	if receipt.docstatus != 1:
		raise Skipped(_("Purchase Receipt {0} is not submitted any more.").format(receipt.name))
	client = client or KoomyoClient.for_connection(connection)
	order_name = event["data"].get("purchase_order")
	po = frappe.get_doc("Purchase Order", order_name) if order_name else None
	if not po or po.koomyo_connection != connection.name or not po.koomyo_id:
		raise Skipped(_("Purchase Receipt {0} is not for an order sent to Koomyo.").format(receipt.name))

	order = client.get("/purchase-orders/" + quote(po.koomyo_id, safe=""))
	if order.get("object") != "purchase_order" or order.get("id") != po.koomyo_id:
		raise Fatal(_("Koomyo answered GET /purchase-orders/{0} with something else.").format(po.koomyo_id))
	if has_shares(order):
		return push_receipt_by_destination(connection, client, po, receipt, order)
	check_receipt_place(connection, po, receipt, order)
	lines = {line.get("ingredient_id"): line for line in order.get("lines") or [] if line.get("id")}

	# Koomyo takes each line's TOTAL received so far and adds only the difference to stock: this
	# delivery's quantity goes on top of what Koomyo already holds for the line.
	delivered: dict[str, Decimal] = {}
	for item in receipt.items:
		if item.purchase_order != po.name:
			continue
		ingredient_id = linked_ingredient(item.item_code, connection)
		line = lines.get(ingredient_id)
		if not line:
			raise Fatal(
				_("Item {0} is not on Koomyo purchase order {1}.").format(item.item_code, po.koomyo_id)
			)
		delivered[line["id"]] = delivered.get(line["id"], Decimal(0)) + Decimal(str(flt(item.stock_qty, 6)))
	totals = {
		line["id"]: money(line.get("quantity_received") or "0", "quantity_received")
		for line in lines.values()
	}
	body = {
		"lines": [
			{"line_id": line_id, "quantity_received": text(totals[line_id] + qty)}
			for line_id, qty in delivered.items()
		]
	}
	if receipt.get("supplier_delivery_note"):
		body["delivery_note"] = str(receipt.supplier_delivery_note)[:80]
	if (
		po.status == "Closed"
	):  # the last delivery and the close in one call; close-short is for a close with none
		body.update(
			{
				"close_short": True,
				"reason_code": "short",
				"reason_note": _("Closed in ERPNext ({0})").format(po.name)[:200],
			}
		)
	before = {r.get("id") for r in order.get("receipts") or []}
	return send_receive(connection, client, po, receipt, body, "receive", before)


@handles("erpnext.purchase_order_cancel", switch="sync_purchasing", scopes=(*WRITE_SCOPES, "refs.write"))
def push_purchase_order_cancel(event: dict, connection, client: KoomyoClient | None = None):
	"""Clean up Koomyo when a Purchase Order is cancelled or deleted in ERPNext.

	What Koomyo holds decides the steps (nothing is guessed, a refused step stops the rest and is reported):
	- draft or ordered: `POST .../cancel` with a reason;
	- received: first reverse every delivery, newest first (`POST .../receipts/{id}/reverse`, reason required: it
	  takes the stock back out), then cancel;
	- partly received: it cannot be cancelled, so it is closed short (`POST .../close-short`);
	- already cancelled: nothing to do.
	Afterwards `DELETE /refs/purchase_order/{id}` removes the link, so ERPNext can reuse the number without
	reaching the old order. The same cleanup runs when the document is deleted (it has the Koomyo id in the event).
	"""
	data = event["data"]
	name = data["name"]
	doc = frappe.db.get_value(
		"Purchase Order", name, ["docstatus", "koomyo_connection", "koomyo_id", "modified"], as_dict=True
	)
	koomyo_id = (doc.koomyo_id if doc else None) or data.get("koomyo_id")
	if doc and (doc.docstatus != 2 or doc.koomyo_connection != connection.name):
		raise Skipped(_("Purchase Order {0} is not a cancelled order of this connection.").format(name))
	if not koomyo_id:
		raise Skipped(_("Purchase Order {0} was never sent to Koomyo.").format(name))
	client = client or KoomyoClient.for_connection(connection)
	stand_in = frappe._dict(
		doctype="Purchase Order",
		name=name,
		koomyo_id=koomyo_id,
		modified=doc.modified if doc else now_datetime(),
	)
	clean_up_order(connection, client, stand_in, event)
	if doc:
		frappe.get_doc("Purchase Order", name).add_comment(
			"Comment", _("Koomyo order {0} cleaned up and its link removed.").format(koomyo_id)
		)
	return ("Purchase Order", name) if doc else None


def clean_up_order(connection, client, po, event: dict):
	from koomyo_erpnext.sync.events import warn

	path = f"/purchase-orders/{quote(po.koomyo_id, safe='')}"
	try:
		order = client.get(path)
	except KoomyoAPIError as e:
		if e.status == 404:
			raise Skipped(_("Koomyo no longer has the order of {0}.").format(po.name))
		raise
	attempt = int(event.get("attempt", 1))
	suffix = "" if attempt <= 1 else f"-{attempt}"
	status = order.get("status")
	if status == "partial":
		if not close_order_short(connection, client, po, order):
			warn(_("The Koomyo order was already closed short."))
		else:
			warn(
				_(
					"The Koomyo order was partly received, so it was closed short, not cancelled: what arrived stays received."
				)
			)
	elif status == "received":
		reversed_count = reverse_deliveries(connection, client, po, order, suffix)
		cancel_koomyo_order(connection, client, po, suffix)
		warn(
			_(
				"The Koomyo order was received: {0} delivery(ies) were reversed (the stock was taken back out), then it was cancelled."
			).format(reversed_count)
		)
	elif status != "cancelled":
		cancel_koomyo_order(connection, client, po, suffix)
	remove_link(connection, client, po, suffix)


def cancel_koomyo_order(connection, client, po, suffix: str):
	try:
		koomyo_write(
			connection,
			client,
			"POST",
			f"/purchase-orders/{quote(po.koomyo_id, safe='')}/cancel",
			{"reason": _("Cancelled in ERPNext ({0})").format(po.name)[:300]},
			# After a refusal a retry needs a fresh key: Koomyo replays a refused answer for the same one.
			idempotency_key("Purchase Order", po.name, po.modified, f"cancel{suffix}"),
		)
	except KoomyoAPIError as e:
		if e.code == "already_cancelled":
			return
		if e.code in ("partially_received", "already_received"):
			raise Fatal(
				_(
					"Koomyo refused to cancel order {0} ({1}): it holds received stock. Nothing more was done and the link was kept. Close or reverse it in Koomyo, then press Retry."
				).format(po.koomyo_id, e.code)
			)
		raise


def reverse_deliveries(connection, client, po, order: dict, suffix: str) -> int:
	"""Reverse every delivery of a received order, newest first (Koomyo reverses only the latest one)."""
	path = f"/purchase-orders/{quote(po.koomyo_id, safe='')}"
	count = 0
	while True:
		live = [r for r in order.get("receipts") or [] if r.get("id") and not r.get("reversed_at")]
		if not live:
			return count
		latest = live[-1]
		try:
			koomyo_write(
				connection,
				client,
				"POST",
				f"{path}/receipts/{quote(latest['id'], safe='')}/reverse",
				{
					"reason": _("Purchase Order {0} deleted or cancelled in ERPNext").format(po.name)[:300],
					"updated_at": order.get("updated_at"),
				},
				idempotency_key("Purchase Order", po.name, po.modified, f"reverse-{latest['id']}{suffix}"),
			)
		except KoomyoAPIError as e:
			if e.code == "stale_version":
				order = client.get(path)
				continue
			if e.code == "already_reversed":
				order = client.get(path)
				continue
			if e.code in ("receipt_distributed", "receipt_returned", "not_latest_receipt"):
				raise Fatal(
					_(
						"Koomyo cannot reverse delivery {0} of order {1} ({2}): part of it already went on to branches by a transfer or back to the supplier. {3} delivery(ies) were reversed before it. The order was not cancelled and its link was kept. Deal with that stock in Koomyo, then press Retry."
					).format(latest["id"], po.koomyo_id, e.code, count)
				)
			raise
		count += 1
		order = client.get(path)


def close_order_short(connection, client, po, order: dict) -> bool:
	"""A partly received order cannot be cancelled: finish it short (one place at a time when it has shares)."""
	if has_shares(order):
		try:
			close_by_destination(connection, client, po, order)
		except Skipped:
			return False
		return True
	return close_short_call(connection, client, po, order)


def remove_link(connection, client, po, suffix: str):
	"""DELETE /refs/purchase_order/{id}: the link to the ERPNext number is removed (the order is not deleted)."""
	try:
		koomyo_write(
			connection,
			client,
			"DELETE",
			f"/refs/purchase_order/{quote(po.koomyo_id, safe='')}",
			None,
			idempotency_key("Purchase Order", po.name, po.modified, f"unlink{suffix}"),
		)
	except KoomyoAPIError as e:
		if e.status != 404:
			raise


def on_purchase_order_trash(doc, method=None):
	"""A Purchase Order sent to Koomyo is deleted in ERPNext: Koomyo is cleaned up first, by the same steps as a cancel.

	ERPNext reuses a Purchase Order number after a delete. Without this the old Koomyo order stays linked to the
	number and a new document under it would be taken for it.
	"""
	if not doc.get("koomyo_connection") or not doc.get("koomyo_id") or doc.get("koomyo_content"):
		return
	connection = frappe.get_cached_doc("Koomyo Connection", doc.koomyo_connection)
	if connection.enabled and connection.sync_purchasing:
		store_outbound(
			connection,
			"erpnext.purchase_order_cancel",
			doc,
			action="delete",
			extra={"koomyo_id": doc.koomyo_id},
		)


@handles("erpnext.purchase_order_close", switch="sync_purchasing", scopes=WRITE_SCOPES)
def push_purchase_close(event: dict, connection, client: KoomyoClient | None = None):
	"""A Purchase Order closed in ERPNext after part of it arrived: finish the Koomyo order short."""
	po = frappe.get_doc("Purchase Order", event["data"]["name"])
	if po.status != "Closed" or po.koomyo_connection != connection.name or not po.koomyo_id:
		raise Skipped(_("Purchase Order {0} is not a closed order sent to Koomyo.").format(po.name))
	client = client or KoomyoClient.for_connection(connection)
	order = client.get("/purchase-orders/" + quote(po.koomyo_id, safe=""))
	if order.get("object") != "purchase_order" or order.get("id") != po.koomyo_id:
		raise Fatal(_("Koomyo answered GET /purchase-orders/{0} with something else.").format(po.koomyo_id))
	if has_shares(order):
		if order.get("status") not in ("ordered", "partial"):
			raise Skipped(
				_("Koomyo purchase order {0} is {1}: nothing left to close short.").format(
					po.koomyo_id, order.get("status")
				)
			)
		return close_by_destination(connection, client, po, order)
	if order.get("status") not in ("ordered", "partial"):
		raise Skipped(
			_("Koomyo purchase order {0} is {1}: nothing left to close short.").format(
				po.koomyo_id, order.get("status")
			)
		)
	if not close_short_call(connection, client, po, order):
		raise Skipped(_("Koomyo purchase order {0} was already closed short.").format(po.koomyo_id))
	return ("Purchase Order", po.name)


def close_short_call(connection, client, po, order: dict, place: tuple[str, str] | None = None) -> bool:
	"""POST /purchase-orders/{id}/close-short: nothing more is arriving (at that place), no stock is booked.

	Docs: a close with nothing delivered; the receive call's `close_short` is for the last delivery plus the close.
	Closes ONE destination; an order whose shares go to several places is closed place by place. Returns False
	when Koomyo says there is nothing left to close (409 nothing_to_receive: a retry is safe).
	"""
	path = f"/purchase-orders/{quote(po.koomyo_id, safe='')}/close-short"
	for attempt in (1, 2):
		body = {
			"reason_code": "short",
			"note": _("Closed in ERPNext ({0})").format(po.name)[:200],
			"updated_at": order.get("updated_at"),
		}
		if place:
			body.update({"destination_type": place[0], "destination_id": place[1]})
		try:
			answer = koomyo_write(
				connection,
				client,
				"POST",
				path,
				body,
				idempotency_key(
					po.doctype, po.name, po.modified, f"close-short-{place[1] if place else 'order'}"
				),
			)
		except KoomyoAPIError as e:
			if e.code == "nothing_to_receive":
				return False
			if e.code == "stale_version" and attempt == 1:
				order["updated_at"] = client.get(path.rsplit("/", 1)[0]).get("updated_at")
				continue
			if e.code == "destination_required":
				raise Fatal(
					_(
						"Koomyo purchase order {0} goes to several places and each is closed on its own, but the place was not named."
					).format(po.koomyo_id)
				)
			raise
		if answer.get("updated_at"):
			order["updated_at"] = answer["updated_at"]  # the next place's close reads the new version
		return True
	return False


def send_receive(connection, client, po, doc, body: dict, action: str, before: set):
	"""POST /purchase-orders/{id}/receive: one delivery, linked to its receipts[] entry."""
	try:
		answer = koomyo_write(
			connection,
			client,
			"POST",
			f"/purchase-orders/{quote(po.koomyo_id, safe='')}/receive",
			body,
			idempotency_key(doc.doctype, doc.name, doc.modified, f"{action}-{po.name}"),
		)
	except KoomyoAPIError as e:
		if e.code == "nothing_to_receive":
			raise Skipped(
				_(
					"Koomyo already holds these quantities for purchase order {0}, so no new delivery was written."
				).format(po.koomyo_id)
			)
		if e.code == "already_received":
			raise Fatal(
				_(
					"Koomyo purchase order {0} is already fully received or closed short, so {1} could not be sent as a delivery; adjust the stock in Koomyo by hand."
				).format(po.koomyo_id, doc.name)
			)
		if e.code == "cancelled":
			raise Fatal(
				_("Koomyo purchase order {0} was cancelled; {1} could not be sent.").format(
					po.koomyo_id, doc.name
				)
			)
		raise
	if answer.get("object") != "purchase_order":
		raise Fatal(_("Koomyo answered the receipt with something else."))
	# The newest delivery on the answer is this one: link it, so the purchase.received event that follows
	# finds it and books nothing.
	new = [r.get("id") for r in answer.get("receipts") or [] if r.get("id") and r.get("id") not in before]
	before.update(new)
	if new and doc.doctype == "Purchase Receipt":
		link_delivery(doc, connection, new[-1])
	return (doc.doctype, doc.name)


def check_receipt_place(connection, po, receipt, order: dict):
	"""An order without shares has one place; a receipt at another Warehouse would land there all the same.

	Koomyo can only split a delivery across places when the order's lines carry shares, so a receipt at a place the
	order is not for is refused before anything is sent (a Warehouse that is not on the Branch Map is not checked).
	"""
	for warehouse in sorted(
		{i.warehouse for i in receipt.items if i.purchase_order == po.name and i.warehouse}
	):
		try:
			kind, place_id = destination_of(connection, warehouse)
		except NeedsMapping:
			continue
		branch, home = order.get("branch_id"), order.get("warehouse_id")
		wrong = (
			(branch and (kind, place_id) != ("branch", branch))
			or (not branch and kind == "branch")
			or (not branch and home and kind == "warehouse" and place_id != home)
		)
		if wrong:
			raise Fatal(
				_(
					"Purchase Receipt {0} receives at Warehouse {1}, which is not the place of Koomyo order {2}, and the order has no shares: Koomyo would put the whole delivery at the order's own place. Koomyo splits a delivery over several places only when the order's lines carry shares. Give the Purchase Order one row for each Warehouse (Update Items), which sends the shares, then press Retry. ERPNext only lets a row be split once the Purchase Receipts of that row are cancelled (cancel them all: each one is reversed in Koomyo if it was received there), then make the receipts again, one for each Warehouse."
				).format(receipt.name, warehouse, po.koomyo_id)
			)


def link_delivery(receipt, connection, delivery_id: str):
	"""Remember which Koomyo delivery this Purchase Receipt is. A receipt that spans several places is several
	deliveries: the first is its Koomyo id, the others are listed beside it, so none is booked twice."""
	receipt.db_set(
		"koomyo_sent", 1, update_modified=False
	)  # sent from ERPNext: only these are reversed there
	if not receipt.koomyo_id:
		receipt.db_set(
			{"koomyo_connection": connection.name, "koomyo_id": delivery_id}, update_modified=False
		)
	elif receipt.koomyo_id != delivery_id:
		known = (receipt.get("koomyo_extra_ids") or "").split()
		if delivery_id not in known:
			receipt.db_set("koomyo_extra_ids", " ".join([*known, delivery_id]), update_modified=False)


def has_shares(order: dict) -> bool:
	return any(line.get("allocations") for line in order.get("lines") or [])


def destination_of(connection, warehouse: str) -> tuple[str, str]:
	"""Koomyo's (destination_type, destination_id) for an ERPNext Warehouse, from the Branch Map."""
	for row in connection.branch_map:
		if row.warehouse == warehouse:
			return ("warehouse" if row.location_type == "Warehouse" else "branch", row.koomyo_branch_id)
	raise NeedsMapping(
		_(
			"Warehouse {0} is not on connection {1}'s Branch Map, so Koomyo cannot be told where the goods arrived. Add a row (a branch, or Location Type Warehouse), then press Retry."
		).format(warehouse, connection.name)
	)


def received_at(order: dict) -> dict[tuple[str, str], dict[str, Decimal]]:
	"""What Koomyo holds per destination per line: the shares' `received_quantity`, summed."""
	out: dict[tuple[str, str], dict[str, Decimal]] = {}
	for line in order.get("lines") or []:
		for share in line.get("allocations") or []:
			key = (share.get("destination_type"), share.get("destination_id"))
			per_line = out.setdefault(key, {})
			per_line[line["id"]] = per_line.get(line["id"], Decimal(0)) + money(
				share.get("received_quantity") or "0", "received_quantity"
			)
	return out


def push_receipt_by_destination(connection, client, po, receipt, order: dict):
	"""An order with branch shares is received once for each place: one call per destination of this receipt."""
	lines = {line.get("ingredient_id"): line for line in order.get("lines") or [] if line.get("id")}
	arrived: dict[tuple[str, str], dict[str, Decimal]] = {}
	for item in receipt.items:
		if item.purchase_order != po.name:
			continue
		line = lines.get(linked_ingredient(item.item_code, connection))
		if not line:
			raise Fatal(
				_("Item {0} is not on Koomyo purchase order {1}.").format(item.item_code, po.koomyo_id)
			)
		place = destination_of(connection, item.warehouse)
		per_line = arrived.setdefault(place, {})
		per_line[line["id"]] = per_line.get(line["id"], Decimal(0)) + Decimal(str(flt(item.stock_qty, 6)))
	before = {r.get("id") for r in order.get("receipts") or []}
	held = received_at(order)
	skipped = 0
	for (kind, place_id), qtys in arrived.items():
		have = held.get((kind, place_id), {})
		body = {
			"destination_type": kind,
			"destination_id": place_id,
			"lines": [
				{"line_id": line_id, "quantity_received": text(have.get(line_id, Decimal(0)) + qty)}
				for line_id, qty in qtys.items()
			],
		}
		if receipt.get("supplier_delivery_note"):
			body["delivery_note"] = str(receipt.supplier_delivery_note)[:80]
		try:
			send_receive(connection, client, po, receipt, body, f"receive-{place_id}", before)
		except Skipped:
			skipped += 1  # nothing new at that place: the other places still go
	if skipped == len(arrived) and arrived:
		raise Skipped(
			_(
				"Koomyo already holds these quantities for purchase order {0}, so no new delivery was written."
			).format(po.koomyo_id)
		)
	return ("Purchase Receipt", receipt.name)


def close_by_destination(connection, client, po, order: dict):
	"""Closed in ERPNext: close short every place that still expects goods (one close-short call for each place)."""
	open_places: list[tuple[str, str]] = []
	for line in order.get("lines") or []:
		for share in line.get("allocations") or []:
			if share.get("closed_short") or money(
				share.get("received_quantity") or "0", "received_quantity"
			) >= money(share.get("quantity") or "0", "quantity"):
				continue  # nothing still expected there
			key = (share.get("destination_type"), share.get("destination_id"))
			if key not in open_places:
				open_places.append(key)
	if not open_places:
		raise Skipped(
			_("Every share of Koomyo purchase order {0} is received or closed short.").format(po.koomyo_id)
		)
	closed = [place for place in open_places if close_short_call(connection, client, po, order, place)]
	if not closed:
		raise Skipped(
			_("Every place of Koomyo purchase order {0} was already closed short.").format(po.koomyo_id)
		)
	return ("Purchase Order", po.name)


@handles("erpnext.supplier", switch="sync_purchasing", scopes=WRITE_SCOPES)
def push_supplier(event: dict, connection, client: KoomyoClient | None = None):
	supplier = frappe.get_doc("Supplier", event["data"]["name"])
	if supplier.koomyo_branch == KOOMYO_ORIGIN:
		raise Skipped(_("Supplier {0} came from Koomyo; Koomyo owns it.").format(supplier.name))
	push_supplier_doc(supplier, connection, client or KoomyoClient.for_connection(connection), force=True)
	return ("Supplier", supplier.name)


@handles("erpnext.item_cost", switch="sync_purchasing", scopes=WRITE_SCOPES)
def push_item_cost(event: dict, connection, client: KoomyoClient | None = None):
	item = frappe.get_doc("Item", event["data"]["name"])
	if item.koomyo_connection != connection.name or not item.koomyo_id:
		raise Skipped(_("Item {0} is not a Koomyo ingredient of this connection.").format(item.name))
	cost = Decimal(str(flt(item.valuation_rate, 6))).quantize(QTY)
	if cost < 0:
		raise Fatal(_("Item {0} has a negative valuation rate.").format(item.name))
	client = client or KoomyoClient.for_connection(connection)
	answer = koomyo_write(
		connection,
		client,
		"PATCH",
		"/ingredients/" + quote(item.koomyo_id, safe=""),
		{"cost_per_unit": text(cost)},
		idempotency_key("Item", item.name, item.modified, "cost"),
	)
	if answer.get("object") != "ingredient":
		raise Fatal(_("Koomyo answered the ingredient cost with something else."))
	return ("Item", item.name)


def push_supplier_doc(supplier, connection, client, force: bool = False) -> str:
	"""Create or update the Supplier in Koomyo (upsert by external_ref) and return its Koomyo id."""
	if supplier.koomyo_connection and supplier.koomyo_connection != connection.name:
		raise NeedsMapping(
			_(
				"Supplier {0} is already linked to connection {1}. Use a separate Supplier for this Company."
			).format(supplier.name, supplier.koomyo_connection)
		)
	if supplier.koomyo_branch == KOOMYO_ORIGIN:
		return supplier.koomyo_id
	if supplier.koomyo_id and not force:
		return supplier.koomyo_id
	body = {
		"name": supplier.supplier_name[:200],
		"active": not supplier.disabled,
		"external_ref": {"system": connection.ref_system or "erpnext", "external_id": supplier.name},
	}
	for field, source in (("phone", "mobile_no"), ("email", "email_id")):
		if supplier.get(source):
			body[field] = supplier.get(source)
	answer = koomyo_write(
		connection,
		client,
		"POST",
		"/suppliers",
		body,
		idempotency_key("Supplier", supplier.name, supplier.modified, "supplier"),
	)
	if answer.get("object") != "supplier" or not answer.get("id"):
		raise Fatal(_("Koomyo answered the supplier with something else."))
	if supplier.koomyo_id != answer["id"]:
		supplier.db_set(
			{"koomyo_connection": connection.name, "koomyo_id": answer["id"]}, update_modified=False
		)
	return answer["id"]


def linked_ingredient(item_code: str, connection) -> str:
	koomyo = frappe.db.get_value(
		"Item", item_code, ["koomyo_connection", "koomyo_id", "is_stock_item"], as_dict=True
	)
	if (
		not koomyo
		or koomyo.koomyo_connection != connection.name
		or not koomyo.koomyo_id
		or not koomyo.is_stock_item
	):
		raise NeedsMapping(
			_(
				"Item {0} is not linked to a Koomyo ingredient of connection {1}. Order only ingredients that came from Koomyo, or link the Item first."
			).format(item_code, connection.name)
		)
	return koomyo.koomyo_id


# ------------------------------------------------------------------ inbound


def koomyo_supplier(connection, supplier: dict, client: KoomyoClient | None = None) -> str:
	"""The Supplier of a Koomyo purchase order, made from Koomyo's whole card when it can be read.

	The order only names the supplier; `GET /suppliers/{id}` has the card (sync/suppliers.py). If that
	cannot be read now, the Supplier is made from the name and filled in by the supplier event later.
	"""
	from koomyo_erpnext.sync.suppliers import apply_card, new_supplier

	if not supplier.get("id"):
		raise Fatal(_("Koomyo sent a purchase order without a supplier."))
	existing = find_existing("Supplier", connection, supplier["id"])
	if existing:
		return existing
	try:
		client = client or KoomyoClient.for_connection(connection)
		card = client.get("/suppliers/" + quote(supplier["id"], safe=""))
		if card.get("object") == "supplier" and card.get("id") == supplier["id"]:
			return apply_card(card, connection)[1]
	except KoomyoAPIError:
		pass
	doc = new_supplier({"id": supplier["id"], "name": supplier.get("name")}, connection)
	doc.insert(ignore_permissions=True)
	return doc.name
