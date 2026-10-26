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
