# Copyright (c) 2026, Sowaan and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document

from koomyo_erpnext.sync.events import enqueue_processing, reset_for_retry

RETRY_ALL_LIMIT = 5000


class KoomyoEventLog(Document):
	pass


def on_doctype_update():
	# One row per event per connection: the duplicate guard for webhook and feed alike.
	frappe.db.add_unique(
		"Koomyo Event Log", ["connection", "event_id"], constraint_name="unique_connection_event"
	)
	frappe.db.add_index("Koomyo Event Log", ["connection", "status", "seq"])


@frappe.whitelist()
def retry(names):
	"""Put Failed or Skipped events back in the queue. Only rows the user may change."""
	if isinstance(names, str):
		names = json.loads(names) if names.startswith("[") else [names]
	connections = set()
	count = 0
	for name in names:
		log = frappe.get_doc("Koomyo Event Log", name)
		log.check_permission("write")
		if reset_for_retry(log):
			connections.add(log.connection)
			count += 1
	for connection in connections:
		enqueue_processing(connection)
	return _("{0} event(s) queued again.").format(count)


@frappe.whitelist()
def retry_all_failed(connection=None):
	"""Queue again every Failed event the user can see (User Permissions on Company apply)."""
	filters = {"status": "Failed"}
	if connection:
		filters["connection"] = connection
	names = frappe.get_list(
		"Koomyo Event Log", filters=filters, pluck="name", limit=RETRY_ALL_LIMIT, order_by="seq asc"
	)
	if not names:
		return _("No failed events to retry.")
	return retry(names)
