"""customer.created / customer.updated -> ERPNext Customer (one way).

Docs: docs.koomyo.com/events/customers, /guides/customers-suppliers, OpenAPI schema Customer.

Only the name, phone and email are copied (CLAUDE.md rule 9: never notes, tags, consent
or do-not-contact). Customer events reach a webhook only when its customer data switch is
on, and the feed only for a key with customers.read. The Koomyo customer is matched by
(koomyo_connection, koomyo_id), so one restaurant's customer is never merged into another's,
and the ERPNext id is written back with PUT /refs/customer/{id} so neither side duplicates.

Events about one customer carry the whole record. An event older (lower seq) than one
already applied for the same customer is skipped, so a late retry never undoes a newer change.
"""

from urllib.parse import quote

import frappe
from frappe import _

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import Fatal, Skipped
from koomyo_erpnext.sync.common import find_existing, setting, stamp
from koomyo_erpnext.sync.events import EVENT_LOG, handles, idempotency_key, koomyo_write

CUSTOMER_SCOPES = ("customers.read", "refs.write")


@handles("customer.created", "customer.updated", switch="sync_customers", scopes=CUSTOMER_SCOPES)
def on_customer_event(event: dict, connection, client: KoomyoClient | None = None):
	data = event["data"]
	if data.get("object") != "customer" or not data.get("id"):
		raise Fatal(_("Koomyo sent something that is not a customer."))
	newer = frappe.db.exists(
		EVENT_LOG,
		{
			"connection": connection.name,
			"entity_id": data["id"],
			"status": "Done",
			"seq": (">", event.get("seq") or 0),
			"event_type": ("in", ("customer.created", "customer.updated")),
		},
	)
	if newer:
		raise Skipped(_("A newer change of this customer was already applied."))

	name = (data.get("name") or "").strip() or (data.get("phone") or "").strip() or data["id"]
	existing = find_existing("Customer", connection, data["id"])
	if existing:
		customer = frappe.get_doc("Customer", existing)
		customer.customer_name = name[:140]
		customer.save(ignore_permissions=True)
		update_primary_contact(customer, data)
	else:
		customer = frappe.new_doc("Customer")
		customer.update(
			{
				"customer_name": name[:140],
				"customer_type": "Individual",
				"customer_group": setting(connection, "customer_group", _("Customer Group")),
				"territory": setting(connection, "territory", _("Territory")),
				"mobile_no": data.get("phone") or None,
				"email_id": data.get("email") or None,
			}
		)
		stamp(customer, connection, data["id"])
		customer.insert(ignore_permissions=True)

	write_reference(connection, client, "customer", data, customer)
	return ("Customer", customer.name)


def update_primary_contact(customer, data: dict):
	if not customer.customer_primary_contact:
		return
	contact = frappe.get_doc("Contact", customer.customer_primary_contact)
	changed = False
	if data.get("phone") and contact.mobile_no != data["phone"]:
		contact.add_phone(data["phone"], is_primary_mobile_no=1)
		contact.mobile_no = data["phone"]
		changed = True
	if data.get("email") and contact.email_id != data["email"]:
		contact.add_email(data["email"], is_primary=1)
		contact.email_id = data["email"]
		changed = True
	if changed:
		contact.save(ignore_permissions=True)


def write_reference(connection, client, entity_type: str, record: dict, doc):
	"""PUT /refs/{type}/{id}: store the ERPNext id on the Koomyo record (once; skipped if it is already there)."""
	system = connection.ref_system or "erpnext"
	from koomyo_erpnext.sync.events import read_only

	if (
		connection.dry_run
		or read_only(connection)
		or (record.get("external_refs") or {}).get(system) == doc.name
	):
		return
	client = client or KoomyoClient.for_connection(connection)
	koomyo_write(
		connection,
		client,
		"PUT",
		f"/refs/{entity_type}/{quote(record['id'], safe='')}",
		{"system": system, "external_id": doc.name},
		idempotency_key(doc.doctype, doc.name, doc.modified, f"ref-{entity_type}"),
	)
