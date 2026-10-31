"""Changes to a purchase order that ERPNext sent to Koomyo: edit it, reverse a receipt, restate costs, return goods.

Docs: docs.koomyo.com/guides/stock-purchasing ("Edit, reverse and return a purchase order"), OpenAPI
PATCH /purchase-orders/{id}, POST .../receipts/{receipt_id}/reverse, .../restate-costs, .../returns.
All need `purchasing.write`, are safe to retry with an Idempotency-Key, are audited under the key's name,
and take the `updated_at` last read (409 stale_version: read again and repeat once).

Every call here starts from the ERPNext document and the order as Koomyo shows it now. Nothing is computed
for Koomyo: totals come back from it. A refusal says what to do and nothing is forced.
"""

from urllib.parse import quote

import frappe
from frappe import _

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import Fatal, KoomyoAPIError, Skipped
from koomyo_erpnext.sync.common import money
from koomyo_erpnext.sync.events import handles, idempotency_key, koomyo_write, store_outbound
from koomyo_erpnext.sync.purchasing import (
	WRITE_SCOPES,
	compare_totals,
	order_lines_for_koomyo,
	order_notes,
	request_line_of,
	tax_fields,
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
	shares = {item.name: request_line_of(item, connection) for item in po.items}
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
	shares = {item.name: request_line_of(item, connection) for item in po.items}
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
