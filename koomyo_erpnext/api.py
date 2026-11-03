"""The webhook receiver.

Address: /api/method/koomyo_erpnext.api.webhook?c=<connection webhook token>

Docs: docs.koomyo.com/webhooks and /webhooks/signatures. It does only what must
happen before answering: find the connection, check the signature with that
connection's secret, check the envelope, store the event, queue the work, answer.
"""

import json

import frappe

from koomyo_erpnext.sync.events import DISPATCH, InvalidEvent, enqueue_processing, store_event
from koomyo_erpnext.utils import verify_signature

MAX_BODY_BYTES = 2 * 1024 * 1024
SIGNATURE_TOLERANCE_SECONDS = 300


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook(**_kwargs):
	# Read the token from the query string only, never from the (untrusted) body.
	token = (frappe.request.args.get("c") or "").strip()
	raw = frappe.request.get_data(cache=True) or b""

	if not token or len(token) > 64:
		return _answer(404, "unknown endpoint")
	if len(raw) > MAX_BODY_BYTES:
		return _answer(413, "body too large")

	connection_name = frappe.db.get_value("Koomyo Connection", {"webhook_token": token}, "name")
	if not connection_name:
		return _answer(404, "unknown endpoint")
	connection = frappe.get_doc("Koomyo Connection", connection_name)

	secret = connection.get_password("webhook_secret", raise_exception=False)
	ok, reason = verify_signature(
		raw,
		frappe.get_request_header("Koomyo-Signature"),
		secret or "",
		tolerance_seconds=SIGNATURE_TOLERANCE_SECONDS,
	)
	if not ok:
		_log_refusal(connection_name, f"signature refused: {reason}")
		return _answer(401, "bad signature")

	if not connection.enabled:
		# Koomyo keeps the event and retries; the feed also picks it up once switched on.
		return _answer(503, "connection is switched off")

	try:
		event = json.loads(raw)
	except ValueError:
		return _answer(400, "body is not JSON")

	header_id = frappe.get_request_header("Koomyo-Event-Id")
	if header_id and isinstance(event, dict) and header_id != event.get("id"):
		return _answer(400, "event id header does not match the body")

	try:
		_name, created = store_event(connection, event, "Webhook")
	except InvalidEvent as e:
		_log_refusal(connection_name, f"event refused: {e}")
		return _answer(400, "invalid event")

	if created and event["type"] in DISPATCH:
		enqueue_processing(connection_name)
	return _answer(200, "ok")


def _answer(status: int, text: str):
	frappe.local.response["http_status_code"] = status
	return text


def _log_refusal(connection_name: str, reason: str):
	# The reason never contains the secret or the signature value.
	frappe.logger("koomyo_erpnext", allow_site=True).warning("webhook %s: %s", connection_name, reason)


@frappe.whitelist(methods=["POST"])
def make_transfer_from_request(material_request: str, source_warehouse: str) -> str:
	"""A draft Material Transfer Stock Entry that fulfils a Koomyo-linked Material Request from a warehouse.

	ERPNext's own `make_stock_entry` maps warehouses by Material Request type and cannot do this for a Purchase
	request, so the entry is built here: one row per request row, from `source_warehouse` to the row's warehouse
	(the branch's), for what is still needed (not ordered, not already transferred). The user reviews it and submits it;
	Koomyo is asked before the submit and told after (sync/transfers_out.py).
	"""
	from frappe import _
	from frappe.utils import flt

	frappe.has_permission("Material Request", "read", material_request, throw=True)
	frappe.has_permission("Stock Entry", "create", throw=True)
	mr = frappe.get_doc("Material Request", material_request)
	if mr.docstatus != 1 or not mr.get("koomyo_connection") or not mr.get("koomyo_id"):
		frappe.throw(
			_("{0} is not a submitted Material Request made from a Koomyo purchase request.").format(mr.name)
		)
	if frappe.db.get_value("Warehouse", source_warehouse, "company") != mr.company:
		frappe.throw(_("Warehouse {0} is not in Company {1}.").format(source_warehouse, mr.company))
	entry = frappe.new_doc("Stock Entry")
	entry.update(
		{
			"company": mr.company,
			"stock_entry_type": "Material Transfer",
			"purpose": "Material Transfer",
			"from_warehouse": source_warehouse,
			"remarks": _("Fulfils Koomyo purchase request lines of {0}.").format(mr.name),
		}
	)
	for item in mr.items:
		if not item.get("koomyo_line_id") or item.warehouse == source_warehouse:
			continue
		moved = frappe.db.sql(
			"""select coalesce(sum(transfer_qty), 0) from `tabStock Entry Detail`
			where material_request_item = %s and docstatus < 2""",
			item.name,
		)[0][0]
		left = flt(item.stock_qty) - flt(item.ordered_qty) - flt(moved)
		if left <= 0:
			continue
		entry.append(
			"items",
			{
				"item_code": item.item_code,
				"qty": left / (flt(item.conversion_factor) or 1),
				"uom": item.uom,
				"conversion_factor": item.conversion_factor,
				"s_warehouse": source_warehouse,
				"t_warehouse": item.warehouse,
				"material_request": mr.name,
				"material_request_item": item.name,
			},
		)
	if not entry.items:
		frappe.throw(_("Nothing of {0} is still needed from a warehouse.").format(mr.name))
	entry.to_warehouse = entry.items[0].t_warehouse
	entry.insert()
	return entry.name
