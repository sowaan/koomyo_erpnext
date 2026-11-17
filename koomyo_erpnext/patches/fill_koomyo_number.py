"""Fill Koomyo Number on the documents made before the field existed.

Nothing is read from Koomyo: the Event Log keeps each event's payload, and the payload carries the number (an order
number, a purchase order number, a request number, a transfer number; a refund names its order). Documents with
no log row left (cleaned up after the retention days) keep an empty number until their record is read again.
Safe to run again: a document that already has a number is never touched.
"""

import json

import frappe

DOCTYPES = (
	"Sales Invoice",
	"Payment Entry",
	"Purchase Order",
	"Purchase Receipt",
	"Material Request",
	"Stock Entry",
)


def number_of(payload) -> str | None:
	try:
		data = (json.loads(payload) or {}).get("data") or {}
	except (TypeError, ValueError):
		return None
	if not isinstance(data, dict):
		return None
	number = data.get("number") or data.get("order_number") or data.get("reference")
	return number[:140] if isinstance(number, str) and number else None


def uuid_like(text: str | None) -> bool:
	return bool(text) and len(text) == 36 and text.count("-") == 4


def execute():
	# Patches run before the fixtures (the custom fields) are imported on `bench migrate`: bring the field in first,
	# or a first run finds no Koomyo Number field and fills nothing.
	from frappe.utils.fixtures import sync_fixtures

	sync_fixtures(app="koomyo_erpnext")
	frappe.clear_cache()
	for doctype in DOCTYPES:
		if not frappe.get_meta(doctype).has_field("koomyo_number"):
			continue
		empty = frappe.get_all(
			doctype,
			filters={"koomyo_id": ("is", "set"), "koomyo_number": ("is", "not set")},
			fields=["name", *(["po_no"] if doctype == "Sales Invoice" else [])],
		)
		for row in empty:
			number = None
			for payload in frappe.get_all(
				"Koomyo Event Log",
				filters={"reference_doctype": doctype, "reference_name": row.name},
				pluck="payload",
				order_by="creation desc",
				limit=5,
			):
				number = number_of(payload)
				if number:
					break
			if not number and doctype == "Sales Invoice" and row.get("po_no"):
				number = row.po_no  # the invoice already kept the order number there
			if not number and doctype == "Purchase Receipt":
				# a delivery is booked from the order's event: it carries the order's number
				order = frappe.db.get_value(
					"Purchase Receipt Item",
					{"parent": row.name, "purchase_order": ("is", "set")},
					"purchase_order",
				)
				number = order and frappe.db.get_value("Purchase Order", order, "koomyo_number")
			if not number and doctype == "Payment Entry":
				reference = frappe.db.get_value("Payment Entry", row.name, "reference_no")
				number = reference if reference and not uuid_like(reference) else None
			if number:
				frappe.db.set_value(doctype, row.name, "koomyo_number", number, update_modified=False)
	frappe.db.commit()
