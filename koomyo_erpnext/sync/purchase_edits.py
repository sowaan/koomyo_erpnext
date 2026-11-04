"""Changes to a purchase order that ERPNext sent to Koomyo: edit it, reverse a receipt, restate costs, return goods.

Docs: docs.koomyo.com/guides/stock-purchasing ("Edit, reverse and return a purchase order"), OpenAPI
PATCH /purchase-orders/{id}, POST .../receipts/{receipt_id}/reverse, .../restate-costs, .../returns.
All need `purchasing.write`, are safe to retry with an Idempotency-Key, are audited under the key's name,
and take the `updated_at` last read (409 stale_version: read again and repeat once).

Every call here starts from the ERPNext document and the order as Koomyo shows it now. Nothing is computed
for Koomyo: totals come back from it. A refusal says what to do and nothing is forced.
"""

from decimal import Decimal
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import flt

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import Fatal, KoomyoAPIError, Skipped
from koomyo_erpnext.sync.common import money
from koomyo_erpnext.sync.events import handles, idempotency_key, koomyo_write, store_outbound
from koomyo_erpnext.sync.purchasing import (
	WRITE_SCOPES,
	compare_totals,
	linked_ingredient,
	order_lines_for_koomyo,
	order_notes,
	order_shares,
	tax_fields,
	text,
)


def order_path(po) -> str:
	return f"/purchase-orders/{quote(po.koomyo_id, safe='')}"


def read_order(client, po) -> dict:
	order = client.get(order_path(po))
	if order.get("object") != "purchase_order" or order.get("id") != po.koomyo_id:
		raise Fatal(_("Koomyo answered GET /purchase-orders/{0} with something else.").format(po.koomyo_id))
	return order


def own_order(po, connection) -> bool:
	"""An order this app sent (not one Koomyo made): the only kind ERPNext changes in Koomyo."""
	return bool(
		po.get("koomyo_connection") == connection.name
		and po.get("koomyo_id")
		and not po.get("koomyo_content")
	)


def retry_key(event: dict, doc, action: str) -> str:
	"""A retry after a refusal needs a fresh key: Koomyo replays a refused answer for the same one."""
	attempt = int(event.get("attempt", 1))
	return idempotency_key(
		doc.doctype, doc.name, doc.modified, action if attempt <= 1 else f"{action}-{attempt}"
	)


# ------------------------------------------------------------------ 1. edit a sent order


def on_purchase_order_update(doc, method=None):
	"""A submitted Purchase Order that was sent to Koomyo was changed (Update Items, or an allowed field)."""
	if doc.docstatus != 1 or not doc.get("koomyo_connection") or not doc.get("koomyo_id"):
		return
	if doc.get("koomyo_content"):
		return  # Koomyo made it: Koomyo owns it
	connection = frappe.get_cached_doc("Koomyo Connection", doc.koomyo_connection)
	if connection.enabled and connection.sync_purchasing:
		store_outbound(connection, "erpnext.purchase_order_edit", doc)


@handles("erpnext.purchase_order_edit", switch="sync_purchasing", scopes=WRITE_SCOPES)
def push_purchase_order_edit(event: dict, connection, client: KoomyoClient | None = None):
	"""PATCH /purchase-orders/{id} with the complete lines and charges, every existing line id and `updated_at`."""
	po = frappe.get_doc("Purchase Order", event["data"]["name"])
	if po.docstatus != 1:
		raise Skipped(_("Purchase Order {0} is not submitted any more.").format(po.name))
	if not own_order(po, connection):
		raise Skipped(_("Purchase Order {0} was not sent to Koomyo by this app.").format(po.name))
	client = client or KoomyoClient.for_connection(connection)
	order = None
	for attempt in (1, 2):
		order = read_order(client, po)
		body = edit_body(po, connection, order)
		if not body:
			raise Skipped(_("Koomyo already has Purchase Order {0} as it is now.").format(po.name))
		try:
			answer = koomyo_write(
				connection,
				client,
				"PATCH",
				order_path(po),
				body,
				retry_key(event, po, f"edit-{order.get('updated_at')}"),
			)
			break
		except KoomyoAPIError as e:
			if e.code == "stale_version" and attempt == 1:
				continue  # changed meanwhile: read it again and repeat the change on that
			explain_edit_refusal(e, po)
			raise
	else:  # pragma: no cover - the loop either breaks or raises
		raise Fatal(_("Koomyo order {0} kept changing: try again.").format(po.koomyo_id))
	if answer.get("object") != "purchase_order":
		raise Fatal(_("Koomyo answered the edit of {0} with something else.").format(po.koomyo_id))
	compare_totals(po, answer)
	return ("Purchase Order", po.name)


def edit_body(po, connection, order: dict) -> dict | None:
	"""What to send to bring Koomyo's order in line with the Purchase Order, or None if there is nothing to send."""
	status = order.get("status")
	if status in ("received", "cancelled"):
		raise Fatal(
			_(
				"Koomyo purchase order {0} is {1}, so it is locked and Purchase Order {2} could not be changed there. Nothing was sent."
			).format(po.koomyo_id, status, po.name)
		)
	residual: list[str] = []
	tax, residual = tax_fields(po, connection)
	expected = str(po.schedule_date) if po.get("schedule_date") else None
	notes = order_notes(po, residual)
	body: dict = {"updated_at": order.get("updated_at")}
	if status == "partial":
		# Part of it arrived: only the notes and the expected date can change (409 partially_received otherwise).
		frozen = frozen_differences(po, connection, order, tax)
		if frozen:
			from koomyo_erpnext.sync.events import warn

			warn(
				_(
					"Goods of Koomyo order {0} have arrived, so its lines, tax and charges are frozen in Koomyo. Not sent: {1}. Only the notes and the expected date were."
				).format(po.koomyo_id, "; ".join(frozen))
			)
		changes = {
			k: v for k, v in (("notes", notes), ("expected_date", expected)) if v and v != order.get(k)
		}
		if not changes:
			return None
		return {**body, **changes}
	shares = order_shares(po, connection)
	lines = order_lines_for_koomyo(po, connection, shares)
	ids = {line.get("ingredient_id"): line.get("id") for line in order.get("lines") or [] if line.get("id")}
	for entry in lines:
		if ids.get(entry["ingredient_id"]):
			entry["id"] = ids[entry["ingredient_id"]]
		# Branch shares are sent again with the line: a line you send replaces that line completely.
		entry.setdefault("allocations", [])
	body.update({"lines": lines, "notes": notes})
	if expected:
		body["expected_date"] = expected
	# The complete list of charges: [] removes them all. The order's tax: a rate of 0 removes it.
	body["charges"] = tax.pop("charges", [])
	body.update({"tax_rate": "0", "tax_name": None, **tax})
	return body


def frozen_differences(po, connection, order: dict, tax: dict) -> list[str]:
	"""What ERPNext changed that a partly received Koomyo order cannot take (said in words, not sent)."""
	seen = []
	shares = order_shares(po, connection)
	wanted = {e["ingredient_id"]: e for e in order_lines_for_koomyo(po, connection, shares)}
	for line in order.get("lines") or []:
		new = wanted.pop(line.get("ingredient_id"), None)
		if not new:
			seen.append(_("{0} removed").format(line.get("ingredient")))
			continue
		if money(new["quantity_ordered"], "quantity") != money(line.get("quantity_ordered"), "quantity"):
			seen.append(_("{0} quantity {1}").format(line.get("ingredient"), new["quantity_ordered"]))
		if money(new["unit_cost"], "unit_cost") != money(line.get("unit_cost"), "unit_cost"):
			seen.append(_("{0} price {1}").format(line.get("ingredient"), new["unit_cost"]))
	seen += [_("new line {0}").format(e["ingredient_id"]) for e in wanted.values()]
	return seen


def explain_edit_refusal(error, po):
	if error.code == "partially_received":
		raise Fatal(
			_(
				"Koomyo order {0} has had goods delivered, so only its notes and expected date can be changed there: supplier, lines, tax, charges and reference are frozen. Purchase Order {1} was changed in ERPNext only."
			).format(po.koomyo_id, po.name)
		)
	if error.code == "po_locked":
		raise Fatal(
			_(
				"Koomyo order {0} is received or cancelled, so it is locked. Purchase Order {1} was changed in ERPNext only."
			).format(po.koomyo_id, po.name)
		)
	if error.code == "stale_version":
		raise Fatal(
			_("Koomyo order {0} changed while it was being edited: press Retry.").format(po.koomyo_id)
		)
	if error.code == "invalid_parameter":
		raise Fatal(
			_(
				"Koomyo refused the edit of Purchase Order {0}: {1}. Check that every row still fills its request line and the shares add up to the line."
			).format(po.name, error.message)
		)


# ------------------------------------------------------------------ 2. cancel a receipt


def koomyo_deliveries(receipt) -> list[str]:
	"""The Koomyo deliveries a Purchase Receipt sent from ERPNext is (one, or several when it spans places)."""
	return [i for i in [receipt.get("koomyo_id"), *(receipt.get("koomyo_extra_ids") or "").split()] if i]


def on_purchase_receipt_cancel(doc, method=None):
	"""A Purchase Receipt that ERPNext sent to Koomyo as a delivery was cancelled: reverse the delivery there.

	Only receipts this app sent (`koomyo_sent`): a receipt made from a delivery Koomyo made stays Koomyo's to
	reverse. Koomyo reverses only the latest delivery of an order.
	"""
	if doc.get("is_return"):
		if doc.get("koomyo_sent"):
			doc.add_comment(
				"Comment",
				_(
					"Cancelled in ERPNext. Koomyo has no call to undo a return, so the goods stay returned there: correct its stock by hand."
				),
			)
		return
	if not doc.get("koomyo_connection") or not doc.get("koomyo_sent") or not koomyo_deliveries(doc):
		return
	connection = frappe.get_cached_doc("Koomyo Connection", doc.koomyo_connection)
	if connection.enabled and connection.sync_purchasing:
		store_outbound(connection, "erpnext.purchase_receipt_reverse", doc)


@handles("erpnext.purchase_receipt_reverse", switch="sync_purchasing", scopes=WRITE_SCOPES)
def push_receipt_reverse(event: dict, connection, client: KoomyoClient | None = None):
	"""POST /purchase-orders/{id}/receipts/{receipt_id}/reverse, latest delivery first, with the reason."""
	receipt = frappe.get_doc("Purchase Receipt", event["data"]["name"])
	if receipt.docstatus != 2:
		raise Skipped(_("Purchase Receipt {0} is not cancelled any more.").format(receipt.name))
	ids = koomyo_deliveries(receipt)
	if not ids or not receipt.get("koomyo_sent"):
		raise Skipped(_("Purchase Receipt {0} was not sent to Koomyo by this app.").format(receipt.name))
	order_name = next((i.purchase_order for i in receipt.items if i.purchase_order), None)
	po = frappe.get_doc("Purchase Order", order_name) if order_name else None
	if not po or po.koomyo_connection != connection.name or not po.koomyo_id:
		raise Skipped(_("Purchase Receipt {0} is not for an order sent to Koomyo.").format(receipt.name))
	client = client or KoomyoClient.for_connection(connection)
	done = 0
	for delivery_id in newest_first(ids, read_order(client, po)):
		done += reverse_delivery(connection, client, po, receipt, delivery_id, event)
	if not done:
		raise Skipped(
			_("The delivery of Purchase Receipt {0} is already reversed in Koomyo.").format(receipt.name)
		)
	return ("Purchase Receipt", receipt.name)


def newest_first(ids: list[str], order: dict) -> list[str]:
	position = {r.get("id"): n for n, r in enumerate(order.get("receipts") or [])}
	return sorted(ids, key=lambda i: position.get(i, -1), reverse=True)


def reverse_delivery(connection, client, po, receipt, delivery_id: str, event: dict) -> int:
	reason = _("Cancelled in ERPNext ({0})").format(receipt.name)[:300]
	for attempt in (1, 2):
		order = read_order(client, po)
		entry = next((r for r in order.get("receipts") or [] if r.get("id") == delivery_id), None)
		if entry and entry.get("reversed_at"):
			return 0
		try:
			answer = koomyo_write(
				connection,
				client,
				"POST",
				f"{order_path(po)}/receipts/{quote(delivery_id, safe='')}/reverse",
				{"reason": reason, "updated_at": order.get("updated_at")},
				retry_key(event, receipt, f"reverse-{delivery_id}-{order.get('updated_at')}"),
			)
			break
		except KoomyoAPIError as e:
			if e.code == "already_reversed":
				return 0
			if e.code == "stale_version" and attempt == 1:
				continue
			explain_reverse_refusal(e, connection, order, delivery_id, receipt)
			raise
	if answer.get("below_zero"):
		from koomyo_erpnext.sync.events import warn

		warn(
			_("Koomyo reversed the delivery, but the goods were already used, so stock there is below zero.")
		)
	return 1


def newer_deliveries(connection, order: dict, delivery_id: str) -> str:
	"""Which newer deliveries must be reversed first, by ERPNext Purchase Receipt where there is one."""
	receipts = order.get("receipts") or []
	ids = [r.get("id") for r in receipts]
	if delivery_id not in ids:
		return ""
	names = []
	for entry in receipts[ids.index(delivery_id) + 1 :]:
		if entry.get("reversed_at"):
			continue
		found = frappe.db.get_value(
			"Purchase Receipt",
			{"koomyo_connection": connection.name, "docstatus": 1, "koomyo_id": entry["id"]},
		) or frappe.db.get_value(
			"Purchase Receipt",
			{
				"koomyo_connection": connection.name,
				"docstatus": 1,
				"koomyo_extra_ids": ("like", f"%{entry['id']}%"),
			},
		)
		names.append(
			found
			or _("the delivery at {0} on {1} (made in Koomyo)").format(
				entry.get("location_name") or _("its place"), str(entry.get("received_at"))[:10]
			)
		)
	return ", ".join(names)


def explain_reverse_refusal(error, connection, order: dict, delivery_id: str, receipt):
	if error.code == "not_latest_receipt":
		newer = newer_deliveries(connection, order, delivery_id)
		raise Fatal(
			_(
				"Purchase Receipt {0} is cancelled in ERPNext, but Koomyo reverses only the latest delivery of an order and later ones depend on this one. Cancel the newer receipt(s) first: {1}. Then press Retry."
			).format(receipt.name, newer or _("(see the order's deliveries in Koomyo)"))
		)
	if error.code == "receipt_distributed":
		raise Fatal(
			_(
				"Purchase Receipt {0} is cancelled in ERPNext, but part of that delivery already went on to branches by a transfer, so Koomyo cannot reverse it. Cancel or resolve those transfers first (in ERPNext the Stock Entries, in Koomyo the transfers), then press Retry."
			).format(receipt.name)
		)
	if error.code == "receipt_returned":
		raise Fatal(
			_(
				"Purchase Receipt {0} is cancelled in ERPNext, but part of that delivery was sent back to the supplier (a return), so Koomyo cannot reverse it. Take the return out first, or adjust the stock in Koomyo by hand."
			).format(receipt.name)
		)
	if error.code == "stale_version":
		raise Fatal(_("Koomyo order changed while the delivery was being reversed: press Retry."))


# ------------------------------------------------------------------ 3. price differences on the invoice


def item_cost(item, inclusive: bool) -> Decimal:
	"""The unit cost of a row in the ingredient's own unit (same rule as the order's unit_cost)."""
	factor = Decimal(str(flt(item.conversion_factor, 9) or 1))
	price = item.base_rate if inclusive else item.base_net_rate
	return Decimal(str(flt(price, 6))) / factor


def price_differences(invoice) -> dict[str, dict[str, Decimal]]:
	"""{Purchase Receipt: {Item: invoiced unit cost}} for rows whose invoiced price differs from the receipt's."""
	inclusive = any(r.get("included_in_print_rate") for r in invoice.get("taxes") or [])
	out: dict[str, dict[str, Decimal]] = {}
	for row in invoice.items:
		if not (row.get("purchase_receipt") and row.get("pr_detail")):
			continue
		received = frappe.db.get_value(
			"Purchase Receipt Item",
			row.pr_detail,
			["base_rate", "base_net_rate", "conversion_factor"],
			as_dict=True,
		)
		if not received:
			continue
		was = item_cost(frappe._dict(received), inclusive)
		now = item_cost(row, inclusive)
		if was != now:
			out.setdefault(row.purchase_receipt, {})[row.item_code] = now
	return out


def on_purchase_invoice_submit(doc, method=None):
	"""A Purchase Invoice that charges another price than the Purchase Receipt it covers: restate that delivery's cost."""
	if doc.get("is_return"):
		return
	differences = price_differences(doc)
	for receipt_name in differences:
		receipt = frappe.db.get_value(
			"Purchase Receipt", receipt_name, ["koomyo_connection", "koomyo_sent"], as_dict=True
		)
		if not receipt or not receipt.koomyo_connection or not receipt.koomyo_sent:
			continue
		connection = frappe.get_cached_doc("Koomyo Connection", receipt.koomyo_connection)
		if (
			connection.enabled
			and connection.sync_purchasing
			and connection.get("restate_costs_from_invoices")
		):
			store_outbound(connection, "erpnext.purchase_invoice_restate", doc)
			return


@handles("erpnext.purchase_invoice_restate", switch="sync_purchasing", scopes=WRITE_SCOPES)
def push_cost_restate(event: dict, connection, client: KoomyoClient | None = None):
	"""POST /purchase-orders/{id}/receipts/{receipt_id}/restate-costs: only the unit cost of the listed lines."""
	invoice = frappe.get_doc("Purchase Invoice", event["data"]["name"])
	if invoice.docstatus != 1:
		raise Skipped(_("Purchase Invoice {0} is not submitted any more.").format(invoice.name))
	if not connection.get("restate_costs_from_invoices"):
		raise Skipped(_("Sending invoice price differences is switched off."))
	client = client or KoomyoClient.for_connection(connection)
	sent = 0
	for receipt_name, costs in price_differences(invoice).items():
		receipt = frappe.get_doc("Purchase Receipt", receipt_name)
		if receipt.get("koomyo_connection") != connection.name or not receipt.get("koomyo_sent"):
			continue
		order_name = next((i.purchase_order for i in receipt.items if i.purchase_order), None)
		po = frappe.get_doc("Purchase Order", order_name) if order_name else None
		if not po or po.koomyo_connection != connection.name or not po.koomyo_id:
			continue
		sent += restate_receipt(connection, client, po, receipt, invoice, costs, event)
	if not sent:
		raise Skipped(_("Koomyo already holds these unit costs, or the receipt was not sent by this app."))
	return ("Purchase Invoice", invoice.name)


def restate_receipt(connection, client, po, receipt, invoice, costs: dict, event: dict) -> int:
	from koomyo_erpnext.sync.events import warn

	count = 0
	for delivery_id in koomyo_deliveries(receipt):
		for attempt in (1, 2):
			order = read_order(client, po)
			entry = next((r for r in order.get("receipts") or [] if r.get("id") == delivery_id), None)
			if not entry:
				raise Fatal(_("Koomyo has no delivery {0} on order {1}.").format(delivery_id, po.koomyo_id))
			if entry.get("reversed_at"):
				raise Fatal(
					_(
						"The delivery of Purchase Receipt {0} is reversed in Koomyo, which cannot restate it. Make the receipt again, then the invoice."
					).format(receipt.name)
				)
			by_ingredient = {l.get("ingredient_id"): l.get("line_id") for l in entry.get("lines") or []}
			lines = []
			for item_code, cost in costs.items():
				line_id = by_ingredient.get(linked_ingredient(item_code, connection))
				if line_id:
					lines.append({"line_id": line_id, "unit_cost": text(cost)})
			if not lines:
				break
			try:
				answer = koomyo_write(
					connection,
					client,
					"POST",
					f"{order_path(po)}/receipts/{quote(delivery_id, safe='')}/restate-costs",
					{
						"reason": _("Supplier invoice {0}").format(invoice.get("bill_no") or invoice.name)[
							:300
						],
						"lines": lines,
						"updated_at": order.get("updated_at"),
					},
					retry_key(event, invoice, f"restate-{delivery_id}-{order.get('updated_at')}"),
				)
			except KoomyoAPIError as e:
				if e.code == "stale_version" and attempt == 1:
					continue
				if e.code == "receipt_reversed":
					raise Fatal(
						_(
							"The delivery of Purchase Receipt {0} is reversed in Koomyo, which cannot restate it."
						).format(receipt.name)
					)
				raise
			count += 1
			rule = answer.get("cost_rule") or {}
			if rule.get("mode") == "ask" and rule.get("suggested"):
				warn(
					_(
						"Koomyo's ingredient cost rule is 'ask': it made a cost suggestion for {0} ingredient(s) that a person accepts in Koomyo."
					).format(len(rule["suggested"]))
				)
			break
	return count


# ------------------------------------------------------------------ 4. goods sent back to the supplier

RETURN_REASONS = {"Damaged": "damaged", "Wrong item": "wrong_item", "Quality": "quality"}


def on_purchase_return_submit(doc, method=None):
	"""A Purchase Return (Purchase Receipt with is_return) against a receipt this app sent: tell Koomyo."""
	if not doc.get("is_return") or not doc.get("return_against"):
		return
	original = frappe.db.get_value(
		"Purchase Receipt", doc.return_against, ["koomyo_connection", "koomyo_sent"], as_dict=True
	)
	if not original or not original.koomyo_connection or not original.koomyo_sent:
		return  # a delivery Koomyo made stays Koomyo's: record the return there
	connection = frappe.get_cached_doc("Koomyo Connection", original.koomyo_connection)
	if connection.enabled and connection.sync_purchasing:
		store_outbound(connection, "erpnext.purchase_return", doc)


@handles("erpnext.purchase_return", switch="sync_purchasing", scopes=WRITE_SCOPES)
def push_purchase_return(event: dict, connection, client: KoomyoClient | None = None):
	"""POST /purchase-orders/{id}/returns: what goes back per order line, from the place it arrived at."""
	doc = frappe.get_doc("Purchase Receipt", event["data"]["name"])
	if doc.docstatus != 1 or not doc.is_return:
		raise Skipped(_("{0} is not a submitted return any more.").format(doc.name))
	if doc.get("koomyo_sent"):
		raise Skipped(_("Return {0} is already in Koomyo.").format(doc.name))
	order_name = next((i.purchase_order for i in doc.items if i.purchase_order), None)
	po = frappe.get_doc("Purchase Order", order_name) if order_name else None
	if not po or po.koomyo_connection != connection.name or not po.koomyo_id:
		raise Skipped(_("Return {0} is not for an order sent to Koomyo.").format(doc.name))
	client = client or KoomyoClient.for_connection(connection)
	for attempt in (1, 2):
		order = read_order(client, po)
		body = return_body(connection, doc, po, order)
		try:
			answer = koomyo_write(
				connection,
				client,
				"POST",
				f"{order_path(po)}/returns",
				body,
				retry_key(event, doc, f"return-{order.get('updated_at')}"),
			)
			break
		except KoomyoAPIError as e:
			if e.code == "stale_version" and attempt == 1:
				continue
			explain_return_refusal(e, doc)
			raise
	known = {r.get("id") for r in order.get("returns") or []}
	made = [r for r in answer.get("returns") or [] if r.get("id") not in known]
	if made:
		doc.db_set("koomyo_connection", connection.name, update_modified=False)
		doc.db_set("koomyo_id", made[-1]["id"], update_modified=False)
	doc.db_set("koomyo_sent", 1, update_modified=False)
	if answer.get("below_zero"):
		from koomyo_erpnext.sync.events import warn

		warn(_("Koomyo took the goods back, but they were already used, so stock there is below zero."))
	return ("Purchase Receipt", doc.name)


def return_body(connection, doc, po, order: dict) -> dict:
	from koomyo_erpnext.sync.purchasing import destination_of

	lines_by_ingredient = {l.get("ingredient_id"): l.get("id") for l in order.get("lines") or []}
	back: dict[str, Decimal] = {}
	places = set()
	for item in doc.items:
		if item.purchase_order != po.name:
			continue
		line_id = lines_by_ingredient.get(linked_ingredient(item.item_code, connection))
		if not line_id:
			raise Fatal(
				_("Item {0} is not on Koomyo purchase order {1}.").format(item.item_code, po.koomyo_id)
			)
		back[line_id] = back.get(line_id, Decimal(0)) + abs(Decimal(str(flt(item.stock_qty, 6))))
		places.add(destination_of(connection, item.warehouse))
	if not back:
		raise Skipped(_("Return {0} has no row of order {1}.").format(doc.name, po.name))
	body = {
		"reason_code": RETURN_REASONS.get(doc.get("koomyo_return_reason") or "", "other"),
		"note": _("ERPNext return {0}").format(doc.name)[:200],
		"lines": [{"line_id": line_id, "quantity": text(qty)} for line_id, qty in back.items()],
		"updated_at": order.get("updated_at"),
	}
	if len(places) > 1:
		raise Fatal(
			_(
				"Return {0} sends goods back from several warehouses at once. Koomyo takes one place per return: make one return for each warehouse."
			).format(doc.name)
		)
	received_at = {(r.get("location_type"), r.get("location_id")) for r in order.get("receipts") or []}
	if len(received_at) > 1:  # the docs: needed when the order was received at more than one place
		kind, place_id = next(iter(places))
		body.update({"destination_type": kind, "destination_id": place_id})
	return body


def explain_return_refusal(error, doc):
	if error.code == "return_exceeds_received":
		raise Fatal(
			_(
				"Koomyo refused return {0}: a line cannot go back for more than arrived at that place, less what already went back and what was sent on to branches. Return less, or take the transfer back first, then press Retry."
			).format(doc.name)
		)
	if error.code == "destination_required":
		raise Fatal(
			_(
				"Koomyo refused return {0}: the order was received at several places, and the return's Warehouse is not one of them on the Branch Map. Check the Warehouse on the return."
			).format(doc.name)
		)
	if error.code == "stale_version":
		raise Fatal(_("Koomyo order changed while the return was sent: press Retry."))
