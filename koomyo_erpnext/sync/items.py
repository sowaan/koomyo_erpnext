"""Koomyo menu items (and ingredients, from the stock sync) as ERPNext Items.

An Item made from Koomyo carries (koomyo_connection, koomyo_id), so one restaurant's
item is never merged into another's, even when two Koomyo instances reuse ids.
"""

import frappe
from frappe import _

from koomyo_erpnext.exceptions import Fatal
from koomyo_erpnext.sync.common import find_existing, setting, stamp

PARTIAL_REFUND_ID = "koomyo-partial-refund"


def default_uom() -> str:
	uom = frappe.db.get_single_value("Stock Settings", "stock_uom") or "Nos"
	if not frappe.db.exists("UOM", uom):
		raise Fatal(_("UOM {0} does not exist. Set the Default Stock UOM in Stock Settings.").format(uom))
	return uom


def menu_item(connection, line: dict) -> str:
	"""The Item for a Koomyo order line: one per Koomyo menu item (or combo), made on first sale."""
	koomyo_id = line.get("item_id") or line.get("combo_id")
	if not koomyo_id:
		raise Fatal(_("Koomyo order line {0} has neither a menu item nor a combo.").format(line.get("id")))
	name = (line.get("name") or koomyo_id)[:140]
	return get_or_create_item(connection, koomyo_id, name, is_stock_item=0, description=line.get("category"))


def partial_refund_item(connection) -> str:
	"""The line used on a proportional credit note (Koomyo refunds carry an amount, not lines)."""
	return get_or_create_item(connection, PARTIAL_REFUND_ID, _("Koomyo Partial Refund"), is_stock_item=0)


def get_or_create_item(
	connection,
	koomyo_id: str,
	item_name: str,
	*,
	is_stock_item: int,
	item_group: str | None = None,
	uom: str | None = None,
	description: str | None = None,
	is_purchase_item: int = 0,
) -> str:
	existing = find_existing("Item", connection, koomyo_id)
	if existing:
		return existing
	group = item_group or setting(connection, "menu_item_group", _("Menu Item Group"))
	item = frappe.new_doc("Item")
	item.item_code = "KMY-" + frappe.generate_hash(length=10).upper()
	item.item_name = item_name
	item.description = description or item_name
	item.item_group = group
	item.stock_uom = uom or default_uom()
	item.is_stock_item = is_stock_item
	item.include_item_in_manufacturing = 0
	item.is_sales_item = 0 if is_stock_item else 1
	item.is_purchase_item = is_purchase_item
	stamp(item, connection, koomyo_id)
	item.insert(ignore_permissions=True)
	return item.name
