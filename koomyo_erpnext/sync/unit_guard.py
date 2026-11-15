"""A row for a Koomyo Item may only use a unit the Item defines.

ERPNext lets any UOM be picked on a purchase or stock row: a unit that is not on the Item's unit table is accepted
with whatever conversion factor is typed. For an Item that came from a Koomyo ingredient the quantity is then sent
to Koomyo (or read from it) in the Item's stock unit, so a wrong unit or a typed factor silently changes the stock.

On documents that carry such Items, every row must use the Item's stock UOM or a UOM of its unit table, and the
conversion factor must be the table's. Other Items and the rest of ERPNext are untouched. A document already saved
is not checked again until it is saved; nothing is changed, only refused with the row, the unit and what is allowed.
"""

import frappe
from frappe import _
from frappe.utils import flt


def allowed_units(item_code: str) -> dict[str, float]:
	"""{UOM: conversion factor} the Item defines: its stock UOM at 1 and every row of its unit table."""
	stock_uom = frappe.get_cached_value("Item", item_code, "stock_uom")
	allowed = {stock_uom: 1.0} if stock_uom else {}
	for row in frappe.get_all(
		"UOM Conversion Detail", filters={"parent": item_code}, fields=["uom", "conversion_factor"]
	):
		allowed.setdefault(row.uom, flt(row.conversion_factor))
	return allowed


def check_row_units(doc, method=None):
	"""validate hook for Material Request, Purchase Order, Purchase Receipt, Purchase Invoice and Stock Entry."""
	seen: dict[str, dict | None] = {}
	for row in doc.get("items") or []:
		code = row.get("item_code")
		if not code:
			continue
		if code not in seen:
			is_koomyo = frappe.get_cached_value("Item", code, "koomyo_id")
			seen[code] = allowed_units(code) if is_koomyo else None
		allowed = seen[code]
		if allowed is None:
			continue
		uom = row.get("uom") or row.get("stock_uom")
		if not uom:
			continue
		listed = ", ".join(
			f"{u} (1 = {flt(f, 6):g} {frappe.get_cached_value('Item', code, 'stock_uom')})"
			for u, f in allowed.items()
		)
		if uom not in allowed:
			frappe.throw(
				_("Row {0}: Item {1} is not defined in {2}. Use one of its own units: {3}.").format(
					row.idx, frappe.bold(code), frappe.bold(uom), listed
				),
				title=_("Unit not defined for this Item"),
			)
		typed = flt(row.get("conversion_factor"))
		if typed and abs(typed - allowed[uom]) > 1e-6:
			frappe.throw(
				_(
					"Row {0}: Item {1} holds {2} {3} in one {4}, but the row says {5}. Use the Item's own conversion."
				).format(
					row.idx,
					frappe.bold(code),
					flt(allowed[uom], 6),
					frappe.get_cached_value("Item", code, "stock_uom"),
					frappe.bold(uom),
					flt(typed, 6),
				),
				title=_("Conversion does not match the Item"),
			)
