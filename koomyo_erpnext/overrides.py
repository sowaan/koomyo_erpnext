"""Hooks that make ERPNext's own buttons aware of Koomyo transfers.

A Koomyo-linked Material Request can be fulfilled from a warehouse by a transfer (api.make_transfer_from_request).
ERPNext does not count that against the request row (its `ordered_qty` follows purchase orders only), so
"Create > Purchase Order" would order the whole request again. `make_purchase_order` is wrapped through Frappe's
`override_whitelisted_methods` hook (no core code is changed): it makes ERPNext's order, then takes off each row
what a submitted transfer already covers, and drops a row that is covered in full.
"""

import frappe
from frappe import _
from frappe.utils import flt


def make_purchase_order(source_name, target_doc=None, args=None):
	from erpnext.stock.doctype.material_request.material_request import make_purchase_order as original

	doclist = original(source_name, target_doc, args)
	if not frappe.db.get_value("Material Request", source_name, "koomyo_id"):
		return doclist
	return take_off_transfers(doclist)


def transferred(material_request_item: str) -> float:
	"""Stock quantity of a Material Request row already on a submitted transfer."""
	return flt(
		frappe.db.sql(
			"""select coalesce(sum(transfer_qty), 0) from `tabStock Entry Detail`
			where material_request_item = %s and docstatus = 1""",
			material_request_item,
		)[0][0]
	)


def take_off_transfers(doclist):
	"""Reduce the Purchase Order's rows by what transfers already cover; drop the rows that are covered."""
	kept, covered = [], []
	for row in doclist.get("items") or []:
		moved = transferred(row.get("material_request_item")) if row.get("material_request_item") else 0
		if not moved:
			kept.append(row)
			continue
		factor = flt(row.get("conversion_factor")) or 1
		left = flt(row.qty) - moved / factor
		if left <= 0:
			covered.append(row.item_code)
			continue
		row.qty = left
		row.stock_qty = left * factor
		kept.append(row)
	if not kept:
		frappe.throw(
			_(
				"Everything on this Material Request is already covered by transfers from a warehouse ({0})."
			).format(", ".join(covered))
		)
	if covered:
		doclist.set("items", [])
		for idx, row in enumerate(kept, start=1):
			row.idx = idx
			doclist.append("items", row)
		frappe.msgprint(
			_("Left out of the order, because transfers from a warehouse cover them: {0}.").format(
				", ".join(covered)
			),
			indicator="blue",
		)
	return doclist
