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

from decimal import Decimal
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import add_to_date, flt, now_datetime

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
	if connection:
		store_outbound(connection, "erpnext.purchase_order", doc)


def on_purchase_receipt_submit(doc, method=None):
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
		return ("Purchase Order", po.name)
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

	lines, seen = [], set()
	for item in po.items:
		ingredient_id = linked_ingredient(item.item_code, connection)
		if ingredient_id in seen:
			raise Fatal(
				_("Purchase Order {0} has Item {1} twice. Koomyo needs one line per ingredient.").format(
					po.name, item.item_code
				)
			)
		seen.add(ingredient_id)
		cost = Decimal(str(flt(item.base_net_rate, 6))) / Decimal(str(flt(item.conversion_factor, 9) or 1))
		lines.append(
			{
				"ingredient_id": ingredient_id,
				"quantity_ordered": text(item.stock_qty),
				"unit_cost": text(cost),
			}
		)

	body = {
		"supplier_id": supplier_id,
		"notes": (po.get("remarks") or _("ERPNext {0}").format(po.name))[:500],
		"lines": lines,
		"external_ref": {"system": connection.ref_system or "erpnext", "external_id": po.name},
	}
	if row.location_type != "Warehouse":
		body["branch_id"] = row.koomyo_branch_id  # leave out to receive into the main warehouse (docs)
	answer = koomyo_write(
		connection,
		client,
		"POST",
		"/purchase-orders",
		body,
		idempotency_key("Purchase Order", po.name, po.modified, "po"),
	)
	if answer.get("object") != "purchase_order" or not answer.get("id"):
		raise Fatal(_("Koomyo answered the purchase order with something else."))
	po.db_set(
		{
			"koomyo_connection": connection.name,
			"koomyo_id": answer["id"],
			"koomyo_branch": body.get("branch_id"),
		}
	)
	return ("Purchase Order", po.name)


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
	if po.status == "Closed":
		body["close_short"] = True
	before = {r.get("id") for r in order.get("receipts") or []}
	return send_receive(connection, client, po, receipt, body, "receive", before)


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
	if order.get("status") != "partial":
		raise Skipped(
			_("Koomyo purchase order {0} is {1}, not partly received: nothing to close short.").format(
				po.koomyo_id, order.get("status")
			)
		)
	# The lines are sent with what Koomyo already holds, so only the closing changes (an empty body would receive everything).
	body = {
		"lines": [
			{
				"line_id": line["id"],
				"quantity_received": text(money(line.get("quantity_received") or "0", "quantity_received")),
			}
			for line in order.get("lines") or []
			if line.get("id")
		],
		"close_short": True,
	}
	before = {r.get("id") for r in order.get("receipts") or []}
	return send_receive(connection, client, po, po, body, "close", before)


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
	if new and doc.doctype == "Purchase Receipt" and not doc.koomyo_id:
		doc.db_set({"koomyo_connection": connection.name, "koomyo_id": new[-1]}, update_modified=False)
	return (doc.doctype, doc.name)


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
