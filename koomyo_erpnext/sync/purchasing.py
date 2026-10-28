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
	if not connection:
		# An order made from Koomyo purchase requests goes to several branches' Warehouses: its connection is the
		# one the Material Requests came from.
		connection = request_connection(doc)
	if connection:
		store_outbound(connection, "erpnext.purchase_order", doc)


def on_purchase_order_cancel(doc, method=None):
	"""A Purchase Order sent to Koomyo was cancelled in ERPNext: Koomyo is told (POST .../cancel)."""
	if not doc.get("koomyo_connection") or not doc.get("koomyo_id") or doc.get("koomyo_content"):
		return  # never sent to Koomyo, or an order Koomyo made itself (its own cancel comes as an event)
	connection = frappe.get_cached_doc("Koomyo Connection", doc.koomyo_connection)
	if connection.enabled and connection.sync_purchasing:
		store_outbound(connection, "erpnext.purchase_order_cancel", doc)


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
		return ("Purchase Order", po.name)
	shares = {item.name: request_line_of(item, connection) for item in po.items}
	with_shares = any(shares.values())
	row = None
	if with_shares:
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
	if with_shares:
		deliver_to = {"Warehouse": "warehouse", "Branches": "branches"}.get(connection.get("deliver_to"))
		if deliver_to:
			body["deliver_to"] = deliver_to
	elif row.location_type != "Warehouse":
		body["branch_id"] = row.koomyo_branch_id  # leave out to receive into the main warehouse (docs)
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
				"po" if event.get("attempt", 1) <= 1 else f"po-{event['attempt']}",
			),
		)
	except KoomyoAPIError as e:
		explain_order_refusal(e, po)
		raise
	if answer.get("object") != "purchase_order" or not answer.get("id"):
		raise Fatal(_("Koomyo answered the purchase order with something else."))
	po.db_set(
		{
			"koomyo_connection": connection.name,
			"koomyo_id": answer["id"],
			"koomyo_branch": body.get("branch_id"),
		}
	)
	compare_totals(po, answer)
	return ("Purchase Order", po.name)


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
			line["shares"][share] = line["shares"].get(share, Decimal(0)) + qty
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
				{"request_line_id": request_line, "quantity": text(qty)}
				for request_line, qty in line["shares"].items()
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


@handles("erpnext.purchase_order_cancel", switch="sync_purchasing", scopes=WRITE_SCOPES)
def push_purchase_order_cancel(event: dict, connection, client: KoomyoClient | None = None):
	"""POST /purchase-orders/{id}/cancel: what the order held goes back to the purchase requests.

	Koomyo cancels a draft or an ordered order. A partly received order answers 409 partially_received (finish
	it with the receive call and close_short), a received one 409 already_received; one already cancelled
	answers 409 already_cancelled, which is the job done.
	"""
	po = frappe.get_doc("Purchase Order", event["data"]["name"])
	if po.docstatus != 2:
		raise Skipped(_("Purchase Order {0} is not cancelled any more.").format(po.name))
	if po.koomyo_connection != connection.name or not po.koomyo_id:
		raise Skipped(_("Purchase Order {0} was never sent to Koomyo.").format(po.name))
	client = client or KoomyoClient.for_connection(connection)
	attempt = int(event.get("attempt", 1))
	try:
		answer = koomyo_write(
			connection,
			client,
			"POST",
			f"/purchase-orders/{quote(po.koomyo_id, safe='')}/cancel",
			{"reason": _("Cancelled in ERPNext ({0})").format(po.name)[:300]},
			# After a refusal a retry needs a fresh key: Koomyo replays a refused answer for the same one.
			idempotency_key(
				"Purchase Order", po.name, po.modified, "cancel" if attempt <= 1 else f"cancel-{attempt}"
			),
		)
	except KoomyoAPIError as e:
		if e.code == "already_cancelled":
			return ("Purchase Order", po.name)
		if e.code == "partially_received":
			raise Fatal(
				_(
					"Koomyo purchase order {0} was partly received, so it cannot be cancelled. Finish it short instead: receive what arrived and close the rest (Close the Purchase Order in ERPNext, or close it short in the Koomyo admin). Purchase Order {1} stays cancelled in ERPNext."
				).format(po.koomyo_id, po.name)
			)
		if e.code == "already_received":
			raise Fatal(
				_(
					"Koomyo purchase order {0} is already received or closed short, so it cannot be cancelled in Koomyo. Purchase Order {1} stays cancelled in ERPNext: adjust the stock in Koomyo by hand."
				).format(po.koomyo_id, po.name)
			)
		raise
	if answer.get("object") != "purchase_order" or answer.get("status") != "cancelled":
		raise Fatal(_("Koomyo answered the cancel of {0} with something else.").format(po.koomyo_id))
	po.add_comment(
		"Comment", _("Cancelled in Koomyo too (order {0}).").format(answer.get("number") or po.koomyo_id)
	)
	return ("Purchase Order", po.name)


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
	if has_shares(order):
		return close_by_destination(connection, client, po, order)
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
	before.update(new)
	if new and doc.doctype == "Purchase Receipt":
		link_delivery(doc, connection, new[-1])
	return (doc.doctype, doc.name)


def link_delivery(receipt, connection, delivery_id: str):
	"""Remember which Koomyo delivery this Purchase Receipt is. A receipt that spans several places is several
	deliveries: the first is its Koomyo id, the others are listed beside it, so none is booked twice."""
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
	"""Closed in ERPNext: finish short every place that still expects goods (the totals so far, plus close_short)."""
	held = received_at(order)
	before = {r.get("id") for r in order.get("receipts") or []}
	open_places: dict[tuple[str, str], dict[str, Decimal]] = {}
	for line in order.get("lines") or []:
		for share in line.get("allocations") or []:
			if share.get("closed_short") or money(
				share.get("received_quantity") or "0", "received_quantity"
			) >= money(share.get("quantity") or "0", "quantity"):
				continue  # nothing still expected there
			key = (share.get("destination_type"), share.get("destination_id"))
			open_places.setdefault(key, {})
	if not open_places:
		raise Skipped(
			_("Every share of Koomyo purchase order {0} is received or closed short.").format(po.koomyo_id)
		)
	for (kind, place_id), _unused in open_places.items():
		per_line = held.get((kind, place_id), {})
		body = {
			"destination_type": kind,
			"destination_id": place_id,
			"lines": [
				{"line_id": line_id, "quantity_received": text(qty)} for line_id, qty in per_line.items()
			],
			"close_short": True,
		}
		send_receive(connection, client, po, po, body, f"close-{place_id}", before)
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
