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

	real_name = (data.get("name") or "").strip()
	existing = find_existing("Customer", connection, data["id"])
	if existing:
		customer = frappe.get_doc("Customer", existing)
		if real_name and customer.customer_name != real_name[:140]:
			# Only a name Koomyo really sent renames the Customer: an event without one never replaces a
			# name (or number) already there with a stand-in.
			customer.customer_name = real_name[:140]
			customer.save(ignore_permissions=True)
			rename_contact(customer)
		update_primary_contact(customer, data)
	else:
		name = display_name(data)
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


def display_name(data: dict) -> str:
	"""The name a new Customer gets: Koomyo's name, else the phone number, else a plain label.

	Koomyo's id is never used as a name; the label carries only its first characters so two unnamed
	customers stay apart.
	"""
	name = (data.get("name") or "").strip() or (data.get("phone") or "").strip()
	return name or _("Koomyo customer {0}").format(str(data["id"])[:8])


def rename_contact(customer):
	"""The primary Contact carries the Customer's name, split like ERPNext splits it when it makes one."""
	if not customer.customer_primary_contact:
		return
	contact = frappe.get_doc("Contact", customer.customer_primary_contact)
	first, _sep, last = customer.customer_name.partition(" ")
	if (contact.first_name, contact.last_name or "") != (first, last):
		contact.first_name, contact.last_name = first, last or None
		contact.save(ignore_permissions=True)


def update_primary_contact(customer, data: dict):
	"""A changed phone or email becomes the primary one of the Customer's Contact (the old one is kept as a row)."""
	if not customer.customer_primary_contact:
		return
	contact = frappe.get_doc("Contact", customer.customer_primary_contact)
	changed = False
	phone, email = (data.get("phone") or "").strip(), (data.get("email") or "").strip()
	if phone and contact.mobile_no != phone:
		row = next((r for r in contact.phone_nos if r.phone == phone), None) or contact.append(
			"phone_nos", {"phone": phone}
		)
		for other in contact.phone_nos:
			other.is_primary_mobile_no = 1 if other is row else 0
			other.is_primary_phone = 0
		changed = True
	if email and contact.email_id != email:
		row = next((r for r in contact.email_ids if r.email_id == email), None) or contact.append(
			"email_ids", {"email_id": email}
		)
		for other in contact.email_ids:
			other.is_primary = 1 if other is row else 0
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
