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
	"""The Item to book a Koomyo order line on.

	A line with a size books on that size's variant (template = the menu item, variant = the
	size, both made on first sale if the menu was not loaded). A line without a size books on
	the menu item (or combo) itself.
	"""
	koomyo_id = line.get("item_id") or line.get("combo_id")
	if not koomyo_id:
		raise Fatal(_("Koomyo order line {0} has neither a menu item nor a combo.").format(line.get("id")))
	name = (line.get("name") or koomyo_id)[:140]
	group = category_group(connection, line.get("category"))
	if line.get("item_id") and line.get("size_id") and line.get("size"):
		return size_variant(connection, line["item_id"], name, line["size_id"], line["size"], group=group)
	code = get_or_create_item(connection, koomyo_id, name, is_stock_item=0, item_group=group)
	if frappe.db.get_value("Item", code, "has_variants"):
		raise Fatal(
			_("Koomyo order line {0} for {1} has no size, but the menu item has sizes in ERPNext.").format(
				line.get("id"), name
			)
		)
	return code


# ------------------------------------------------------------------ sizes as variants

SIZE_ATTRIBUTE = "Koomyo Size"


def ensure_size_attribute():
	if not frappe.db.exists("Item Attribute", SIZE_ATTRIBUTE):
		frappe.get_doc(
			{"doctype": "Item Attribute", "attribute_name": SIZE_ATTRIBUTE, "item_attribute_values": []}
		).insert(ignore_permissions=True)


def size_value(size_name: str) -> str:
	"""Make sure the size exists as a value of the Koomyo Size attribute (abbreviation unique)."""
	size_name = size_name.strip()[:140]
	ensure_size_attribute()
	attribute = frappe.get_doc("Item Attribute", SIZE_ATTRIBUTE)
	if any(v.attribute_value == size_name for v in attribute.item_attribute_values):
		return size_name
	taken = {v.abbr for v in attribute.item_attribute_values}
	base = "".join(ch for ch in size_name.upper() if ch.isalnum())[:10] or "SIZE"
	abbr, n = base, 1
	while abbr in taken:
		n += 1
		abbr = f"{base[:8]}{n}"
	attribute.append("item_attribute_values", {"attribute_value": size_name, "abbr": abbr})
	attribute.save(ignore_permissions=True)
	return size_name


def menu_template(
	connection, item_id: str, name: str, group: str | None = None, description: str | None = None
) -> str | None:
	"""The template Item of a sized menu item. None when an existing Item already has history
	as a plain Item (ERPNext cannot turn it into a template): then sizes book on it as before."""
	existing = find_existing("Item", connection, item_id)
	if existing:
		item = frappe.get_doc("Item", existing)
		if item.has_variants:
			return item.name
		if has_history(item.name):
			return None
		ensure_size_attribute()
		# A template cannot have prices in ERPNext; its sizes get them.
		frappe.db.delete("Item Price", {"item_code": item.name})
		item.has_variants = 1
		item.set("attributes", [{"attribute": SIZE_ATTRIBUTE}])
		item.save(ignore_permissions=True)
		return item.name
	code = get_or_create_item(
		connection, item_id, name, is_stock_item=0, item_group=group, description=description, has_variants=1
	)
	return code


def size_variant(
	connection,
	item_id: str,
	name: str,
	size_id: str,
	size_name: str,
	group: str | None = None,
	description: str | None = None,
) -> str:
	"""The variant Item of one size, keyed by Koomyo's size id."""
	existing = find_existing("Item", connection, size_id)
	if existing:
		return existing
	template = menu_template(connection, item_id, name, group=group, description=description)
	if not template:
		return find_existing("Item", connection, item_id)
	from erpnext.controllers.item_variant import create_variant, get_variant

	args = {SIZE_ATTRIBUTE: size_value(size_name)}
	same_size = get_variant(template, args)
	if same_size:
		# This size already has a variant (under another Koomyo size id): book on it.
		if not frappe.db.get_value("Item", same_size, "koomyo_id"):
			frappe.db.set_value(
				"Item", same_size, {"koomyo_connection": connection.name, "koomyo_id": size_id}
			)
		return same_size
	variant = create_variant(template, args)
	variant.item_name = f"{frappe.db.get_value('Item', template, 'item_name')} ({size_name})"[:140]
	variant.description = variant.item_name
	stamp(variant, connection, size_id)
	variant.insert(ignore_permissions=True)
	return variant.name


def has_history(item_code: str) -> bool:
	return any(
		frappe.db.exists(doctype, {"item_code": item_code})
		for doctype in ("Sales Invoice Item", "POS Invoice Item", "Stock Ledger Entry", "Purchase Order Item")
	)


def category_group(connection, category_name: str | None, parent_name: str | None = None) -> str:
	"""A leaf Item Group per Koomyo category, under one '<connection> Menu' group."""
	if not category_name:
		return setting(connection, "menu_item_group", _("Menu Item Group"))
	parent = parent_name or f"{connection.name} Menu"[:140]
	if not frappe.db.exists("Item Group", parent):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": parent,
				"parent_item_group": "All Item Groups",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)
	group = f"{category_name} ({connection.name})"[:140]
	if not frappe.db.exists("Item Group", group):
		frappe.get_doc(
			{"doctype": "Item Group", "item_group_name": group, "parent_item_group": parent, "is_group": 0}
		).insert(ignore_permissions=True)
	return group


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
	has_variants: int = 0,
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
	if has_variants:
		ensure_size_attribute()  # the attribute must exist before a template can name it
		item.has_variants = 1
		item.set("attributes", [{"attribute": SIZE_ATTRIBUTE}])
	stamp(item, connection, koomyo_id)
	item.insert(ignore_permissions=True)
	return item.name
