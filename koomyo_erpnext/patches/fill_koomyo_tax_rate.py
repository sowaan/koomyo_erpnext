"""Fill Koomyo Tax Rate on the invoices made before the field existed.

The rate comes from the order's logged event (`amounts.tax_rate`); when no log row is left, from the tax row's own
text ("Sales tax 15.00%"); a credit note takes its sale's rate. Nothing is read from Koomyo. Safe to run again:
an invoice that already has a rate is never touched.
"""

import json
import re

import frappe

TEXT = re.compile(r"Sales tax\s+(-?[0-9]+(?:\.[0-9]+)?)\s*%", re.IGNORECASE)


def rate_from_payload(payload) -> str | None:
	try:
		data = (json.loads(payload) or {}).get("data") or {}
	except (TypeError, ValueError):
		return None
	amounts = data.get("amounts") if isinstance(data, dict) else None
	rate = amounts.get("tax_rate") if isinstance(amounts, dict) else None
	return str(rate)[:140] if rate not in (None, "") else None


def execute():
	from frappe.utils.fixtures import sync_fixtures

	sync_fixtures(
		app="koomyo_erpnext"
	)  # patches run before the fixtures on migrate: bring the field in first
	frappe.clear_cache()
	if not frappe.get_meta("Sales Invoice").has_field("koomyo_tax_rate"):
		return
	rows = frappe.get_all(
		"Sales Invoice",
		filters={"koomyo_id": ("is", "set"), "koomyo_tax_rate": ("is", "not set"), "docstatus": ("<", 2)},
		fields=["name", "is_return", "return_against"],
		order_by="is_return asc, creation asc",  # sales first, so a credit note finds its sale's rate
	)
	for row in rows:
		rate = None
		if row.is_return and row.return_against:
			rate = frappe.db.get_value("Sales Invoice", row.return_against, "koomyo_tax_rate")
		if not rate:
			for payload in frappe.get_all(
				"Koomyo Event Log",
				filters={"reference_doctype": "Sales Invoice", "reference_name": row.name},
				pluck="payload",
				order_by="creation desc",
				limit=5,
			):
				rate = rate_from_payload(payload)
				if rate:
					break
		if not rate:
			for description in frappe.get_all(
				"Sales Taxes and Charges", filters={"parent": row.name}, pluck="description"
			):
				found = TEXT.search(description or "")
				if found:
					rate = found.group(1)
					break
		if rate:
			frappe.db.set_value("Sales Invoice", row.name, "koomyo_tax_rate", rate, update_modified=False)
	frappe.db.commit()
