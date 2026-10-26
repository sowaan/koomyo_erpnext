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

Koomyo -> ERPNext:
- purchase.received for an order made in ERPNext and not yet received there
  (received in the Koomyo admin) -> Purchase Receipt from the Purchase Order.
"""

from decimal import Decimal
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import flt

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import Fatal, KoomyoAPIError, NeedsMapping, Skipped
from koomyo_erpnext.sync.common import find_existing, money, posting_date, stamp
from koomyo_erpnext.sync.events import handles, idempotency_key, koomyo_write, store_outbound

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


def on_supplier_update(doc, method=None):
	if doc.get("koomyo_connection"):
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
	line_ids = {line.get("ingredient_id"): line.get("id") for line in order.get("lines") or []}

	received: dict[str, Decimal] = {}
	for item in receipt.items:
		if item.purchase_order != po.name:
			continue
		ingredient_id = linked_ingredient(item.item_code, connection)
		line_id = line_ids.get(ingredient_id)
		if not line_id:
			raise Fatal(
				_("Item {0} is not on Koomyo purchase order {1}.").format(item.item_code, po.koomyo_id)
			)
		received[line_id] = received.get(line_id, Decimal(0)) + Decimal(str(flt(item.stock_qty, 6)))
	body = {"lines": [{"line_id": k, "quantity_received": text(v)} for k, v in received.items()]}
	try:
		answer = koomyo_write(
			connection,
			client,
			"POST",
			f"/purchase-orders/{quote(po.koomyo_id, safe='')}/receive",
			body,
			idempotency_key("Purchase Receipt", receipt.name, receipt.modified, f"receive-{po.name}"),
		)
	except KoomyoAPIError as e:
		if e.code == "already_received":
			raise Fatal(
				_(
					"Koomyo purchase order {0} was already received. Koomyo takes one receipt per order, so Purchase Receipt {1} could not be sent; adjust the stock in Koomyo by hand."
				).format(po.koomyo_id, receipt.name)
			)
		raise
	if answer.get("object") != "purchase_order":
		raise Fatal(_("Koomyo answered the receipt with something else."))
	return ("Purchase Receipt", receipt.name)


@handles("erpnext.supplier", switch="sync_purchasing", scopes=WRITE_SCOPES)
def push_supplier(event: dict, connection, client: KoomyoClient | None = None):
	supplier = frappe.get_doc("Supplier", event["data"]["name"])
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


@handles("purchase.received", switch="sync_purchasing", scopes=WRITE_SCOPES)
def on_purchase_received(event: dict, connection):
	order = event["data"]
	if order.get("object") != "purchase_order" or not order.get("id"):
		raise Fatal(_("Koomyo sent something that is not a purchase order."))
	existing = find_existing("Purchase Receipt", connection, order["id"])
	if existing:
		return ("Purchase Receipt", existing)

	po_name = find_existing("Purchase Order", connection, order["id"])
	ref = (order.get("external_refs") or {}).get(connection.ref_system or "erpnext")
	if not po_name and ref and frappe.db.exists("Purchase Order", ref):
		po_name = ref
	if not po_name:
		raise Skipped(
			_(
				"Purchase order {0} was made in Koomyo, not in ERPNext. Book its receipt in ERPNext by hand if needed."
			).format(order["id"])
		)
	po = frappe.get_doc("Purchase Order", po_name)
	if po.company != connection.company:
		raise NeedsMapping(
			_("Purchase Order {0} belongs to {1}, not to {2}.").format(
				po.name, po.company, connection.company
			)
		)
	already = frappe.get_all(
		"Purchase Receipt Item", filters={"purchase_order": po.name, "docstatus": 1}, pluck="parent", limit=1
	)
	if already:
		raise Skipped(
			_("Purchase Order {0} is already received in ERPNext ({1}).").format(po.name, already[0])
		)

	from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

	quantities = {}
	for line in order.get("lines") or []:
		quantities[line.get("ingredient_id")] = quantities.get(line.get("ingredient_id"), Decimal(0)) + money(
			line.get("quantity_received"), "quantity_received"
		)
	receipt = make_purchase_receipt(po.name)
	for item in list(receipt.items):
		ingredient_id = frappe.db.get_value("Item", item.item_code, "koomyo_id")
		stock_qty = quantities.get(ingredient_id, Decimal(0))
		if not stock_qty:
			receipt.remove(item)
			continue
		factor = Decimal(str(flt(item.conversion_factor, 9) or 1))
		item.qty = item.received_qty = flt(str(stock_qty / factor), 6)
	if not receipt.items:
		raise Skipped(_("Nothing was received on purchase order {0}.").format(order["id"]))
	receipt.posting_date = posting_date(order)
	receipt.set_posting_time = 1
	stamp(receipt, connection, order["id"], order.get("branch_id"))
	receipt.insert(ignore_permissions=True)
	receipt.submit()
	return ("Purchase Receipt", receipt.name)
