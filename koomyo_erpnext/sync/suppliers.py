"""Koomyo suppliers -> ERPNext Supplier, Address, Contact and Payment Terms Template.

Docs: docs.koomyo.com/events/master-data (supplier.created, supplier.updated), /guides/customers-suppliers,
OpenAPI schema Supplier, GET /suppliers/{id} (scope purchasing.read).

The whole card is read: code, contact, phones, WhatsApp, email, website, address, tax numbers and
status, payment terms, credit limit, currency, lead time, minimum order, delivery days, tags,
preferred, rating, notes and whether it is active. Bank details are never in the API.

Where it lands:

- Supplier: name, `tax_number` -> Tax ID, website, default currency, disabled when not active,
  the payment terms as a Payment Terms Template, and a plain "Koomyo card" text for what ERPNext has
  no field for (code, second tax number and status, credit limit, lead time, minimum order,
  delivery days, tags, preferred, rating, notes).
- Address: one Billing address when the card has a first address line, a city and a known country.
- Contact: one contact with the phones, WhatsApp and email.
- payment terms: cod -> "Koomyo Cash on Delivery", advance -> "Koomyo Payment in Advance", net N days
  -> "Koomyo Net N". The templates are shared and made once.

A supplier made in ERPNext and sent to Koomyo is ERPNext's own: Koomyo echoes it back as
supplier.updated, and that echo changes nothing. Koomyo's text is untrusted: it is escaped before it
goes into a text field. A copy older than the one on file changes nothing.
"""

import frappe
from frappe import _
from frappe.utils import escape_html, get_datetime

from koomyo_erpnext.exceptions import Fatal, Skipped
from koomyo_erpnext.sync.common import find_existing, setting, stamp
from koomyo_erpnext.sync.events import handles

SCOPES = ("purchasing.read",)
# Suppliers made from a Koomyo record carry this in koomyo_branch (unused on Suppliers), so they are
# never pushed back to Koomyo, which already owns them (sync/purchasing.py).
KOOMYO_ORIGIN = "origin:koomyo"
DAYS = {
	"mon": "Monday",
	"tue": "Tuesday",
	"wed": "Wednesday",
	"thu": "Thursday",
	"fri": "Friday",
	"sat": "Saturday",
	"sun": "Sunday",
}


@handles("supplier.created", "supplier.updated", switch="book_koomyo_purchases", scopes=SCOPES)
def on_supplier(event: dict, connection):
	return apply_card(event["data"], connection)


def apply_card(card: dict, connection) -> tuple[str, str]:
	if card.get("object") != "supplier" or not card.get("id"):
		raise Fatal(_("Koomyo sent something that is not a supplier."))
	name = existing_supplier(card, connection)
	if name:
		doc = frappe.get_doc("Supplier", name)
		if doc.get("koomyo_branch") != KOOMYO_ORIGIN:
			raise Skipped(
				_(
					"Supplier {0} was made in ERPNext, which stays the source for it. Koomyo's copy changes nothing."
				).format(doc.name)
			)
		if is_stale(doc, card):
			return ("Supplier", doc.name)
	else:
		doc = new_supplier(card, connection)
	notes = fill_supplier(doc, card, connection)
	save(doc)
	if card.get("updated_at"):
		doc.db_set("koomyo_updated_at", card["updated_at"], update_modified=False)
	notes += fill_address(doc, card, connection)
	notes += fill_contact(doc, card, connection)
	if notes:
		doc.add_comment("Comment", _("From Koomyo: ") + "; ".join(notes))
	return ("Supplier", doc.name)


def save(doc):
	if doc.is_new():
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)


def existing_supplier(card: dict, connection) -> str | None:
	name = find_existing("Supplier", connection, card["id"])
	ref = (card.get("external_refs") or {}).get(connection.ref_system or "erpnext")
	if not name and ref and frappe.db.exists("Supplier", ref):
		return ref
	return name


def is_stale(doc, card: dict) -> bool:
	seen, new = doc.get("koomyo_updated_at"), card.get("updated_at")
	return bool(seen and new and get_datetime(new) < get_datetime(seen))


def new_supplier(card: dict, connection):
	name = ((card.get("name") or "").strip() or _("Koomyo supplier {0}").format(str(card["id"])[:8]))[:140]
	if frappe.db.exists("Supplier", name):
		# A Supplier of that name is already in ERPNext and is not this Koomyo supplier: never merge.
		name = f"{name[:100]} ({connection.name})"
	doc = frappe.new_doc("Supplier")
	doc.update(
		{
			"supplier_name": name,
			"supplier_group": setting(connection, "supplier_group", _("Supplier Group")),
			"supplier_type": "Company",
		}
	)
	stamp(doc, connection, card["id"], KOOMYO_ORIGIN)
	return doc


def fill_supplier(doc, card: dict, connection) -> list[str]:
	"""Copy the card onto the Supplier. Returns notes for what could not be placed."""
	notes = []
	if not doc.is_new() and card.get("name") and card["name"] != doc.supplier_name:
		taken = frappe.db.exists("Supplier", {"supplier_name": card["name"], "name": ("!=", doc.name)})
		if not taken:
			doc.supplier_name = card["name"][:140]
	doc.tax_id = (card.get("tax_number") or "")[:140] or None
	doc.website = (card.get("website") or "")[:140] or None
	doc.disabled = 0 if card.get("active", True) else 1
	currency = card.get("currency")
	if currency:
		if frappe.db.exists("Currency", {"name": currency, "enabled": 1}):
			doc.default_currency = currency
		else:
			notes.append(_("currency {0} is not an enabled ERPNext Currency").format(currency))
	template = payment_terms_template(card)
	if template:
		doc.payment_terms = template
	doc.supplier_details = card_text(card)
	return notes


def card_text(card: dict) -> str:
	"""What ERPNext has no field for, as plain lines (escaped: the text is Koomyo's)."""
	lines = []

	def add(label, value):
		if value not in (None, "", []):
			lines.append(f"{label}: {escape_html(str(value))}")

	add(_("Koomyo supplier code"), card.get("code"))
	add(_("Second tax number"), card.get("tax_number2"))
	add(_("Tax status"), card.get("tax_status"))
	add(_("Credit limit"), card.get("credit_limit"))
	add(_("Lead time (days)"), card.get("lead_time_days"))
	add(_("Minimum order"), card.get("min_order_value"))
	add(_("Delivery days"), ", ".join(DAYS.get(d, d) for d in card.get("delivery_days") or []))
	add(_("Tags"), ", ".join(card.get("tags") or []))
	add(_("Preferred supplier"), _("Yes") if card.get("preferred") else None)
	add(_("Rating (1 to 5)"), card.get("rating"))
	add(_("Notes"), card.get("notes"))
	return "\n".join(lines)


# ------------------------------------------------------------------ payment terms


def payment_terms_template(card: dict) -> str | None:
	"""cod / advance / net N days as a Payment Terms Template, made once and shared."""
	terms = card.get("payment_terms")
	if not terms:
		return None
	if terms == "net":
		days = card.get("payment_days")
		if not isinstance(days, int) or isinstance(days, bool) or days < 0:
			raise Fatal(_("Supplier {0}: net payment terms need a number of days.").format(card.get("name")))
		return terms_template(f"Koomyo Net {days}", days, _("Pay {0} days after the invoice").format(days))
	if terms == "cod":
		return terms_template("Koomyo Cash on Delivery", 0, _("Pay in cash when the goods arrive"))
	if terms == "advance":
		return terms_template("Koomyo Payment in Advance", 0, _("Pay before the goods are sent"))
	raise Fatal(_("Supplier {0}: unknown payment terms {1!r}.").format(card.get("name"), terms))


def terms_template(name: str, credit_days: int, description: str) -> str:
	if not frappe.db.exists("Payment Term", name):
		frappe.get_doc(
			{
				"doctype": "Payment Term",
				"payment_term_name": name,
				"invoice_portion": 100,
				"due_date_based_on": "Day(s) after invoice date",
				"credit_days": credit_days,
				"description": description,
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Payment Terms Template", name):
		frappe.get_doc(
			{
				"doctype": "Payment Terms Template",
				"template_name": name,
				"terms": [
					{
						"payment_term": name,
						"description": description,
						"invoice_portion": 100,
						"due_date_based_on": "Day(s) after invoice date",
						"credit_days": credit_days,
					}
				],
			}
		).insert(ignore_permissions=True)
	return name


# ------------------------------------------------------------------ address and contact


def country_name(text: str | None) -> str | None:
	"""An ERPNext Country matching Koomyo's text (capitals ignored), or None: never guessed."""
	if not text:
		return None
	return frappe.db.get_value("Country", {"name": text.strip()}) or next(
		iter(frappe.get_all("Country", filters={"name": ("like", text.strip())}, pluck="name", limit=1)), None
	)


def linked(doctype: str, supplier, connection):
	return frappe.db.get_value(
		doctype, {"koomyo_connection": connection.name, "koomyo_id": supplier.koomyo_id}
	)


def fill_address(supplier, card: dict, connection) -> list[str]:
	if not any(card.get(k) for k in ("address", "address_line2", "city", "region", "country", "postal_code")):
		return []
	line1, city, country = card.get("address"), card.get("city"), country_name(card.get("country"))
	missing = [
		label
		for label, value in (
			(_("first address line"), line1),
			(_("city"), city),
			(_("a known country"), country),
		)
		if not value
	]
	if missing:
		return [_("address not made: Koomyo's card has no {0}").format(", ".join(missing))]
	name = linked("Address", supplier, connection)
	doc = frappe.get_doc("Address", name) if name else frappe.new_doc("Address")
	doc.update(
		{
			"address_title": supplier.supplier_name[:140],
			"address_type": "Billing",
			"address_line1": line1[:140],
			"address_line2": (card.get("address_line2") or "")[:140] or None,
			"city": city[:140],
			"state": (card.get("region") or "")[:140] or None,
			"country": country,
			"pincode": (card.get("postal_code") or "")[:140] or None,
			"phone": card.get("phone"),
			"email_id": card.get("email"),
		}
	)
	if not name:
		doc.append("links", {"link_doctype": "Supplier", "link_name": supplier.name})
		stamp(doc, connection, supplier.koomyo_id, KOOMYO_ORIGIN)
	save(doc)
	return []


def fill_contact(supplier, card: dict, connection) -> list[str]:
	phones = [
		("phone", card.get("phone")),
		("alt_phone", card.get("alt_phone")),
		("whatsapp", card.get("whatsapp")),
	]
	if not (card.get("contact_name") or card.get("email") or any(p for _k, p in phones)):
		return []
	name = linked("Contact", supplier, connection)
	doc = frappe.get_doc("Contact", name) if name else frappe.new_doc("Contact")
	doc.first_name = (card.get("contact_name") or supplier.supplier_name)[:140]
	doc.company_name = supplier.supplier_name[:140]
	doc.set("phone_nos", [])
	first = True
	for _kind, number in phones:
		if number:
			doc.append(
				"phone_nos",
				{
					"phone": str(number)[:140],
					"is_primary_phone": 1 if first else 0,
					"is_primary_mobile_no": 0,
				},
			)
			first = False
	doc.set("email_ids", [])
	if card.get("email"):
		doc.append("email_ids", {"email_id": card["email"][:140], "is_primary": 1})
	if not name:
		doc.append("links", {"link_doctype": "Supplier", "link_name": supplier.name})
		stamp(doc, connection, supplier.koomyo_id, KOOMYO_ORIGIN)
	save(doc)
	return []
