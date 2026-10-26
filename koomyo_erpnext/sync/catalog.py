"""Load Koomyo's menu and ingredients into ERPNext Items, up front.

Docs: docs.koomyo.com/read-data, OpenAPI GET /menu-items, GET /categories, GET /ingredients,
schemas MenuItem, Category, Ingredient.

Live sync makes an Item the first time a menu item is sold or an ingredient moves. This
loads them all at once, with the same identity, (koomyo_connection, koomyo_id), so loading
twice, or a sale afterwards, never makes a second Item. Koomyo owns the menu: names,
descriptions, categories and whether an item is active are copied over on every load.

- Menu items -> non-stock Items in one leaf Item Group per Koomyo category (under a
  "<connection> Menu" group), with the base price on the connection's selling price list.
- Ingredients -> stock Items with the UOM Map unit and Koomyo's cost as valuation rate.

Koomyo's text is untrusted: it is escaped before it goes into an Item description.
References are written back to Koomyo (PUT /refs) only on a real load with Dry Run off.
"""

import time

import frappe
from frappe import _
from frappe.utils import escape_html, flt, now_datetime

from koomyo_erpnext.client import KoomyoClient, check_list_page
from koomyo_erpnext.exceptions import Fatal, KoomyoAPIError, NeedsMapping
from koomyo_erpnext.sync.common import find_existing, money, setting
from koomyo_erpnext.sync.customers import write_reference
from koomyo_erpnext.sync.items import get_or_create_item
from koomyo_erpnext.sync.stock import ingredient_item

SLEEP = time.sleep


def load_catalog(connection_name: str, preview: bool = False) -> dict:
	"""Background job. Returns (and stores on the connection) a summary of what it did."""
	connection = frappe.get_doc("Koomyo Connection", connection_name)
	if preview:
		connection.dry_run = 1  # in memory only: nothing is written to Koomyo, everything is rolled back
	client = KoomyoClient.for_connection(connection)
	summary = {
		"menu": {"created": 0, "updated": 0, "unchanged": 0},
		"ingredients": {"created": 0, "updated": 0, "unchanged": 0},
		"problems": [],
	}
	try:
		categories = {c["id"]: c for c in read_list(client, "/categories", {})}
		menu = read_list(client, "/menu-items", {"limit": 200})
		ingredients = read_list(client, "/ingredients", {"limit": 500})
	except KoomyoAPIError as e:
		from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_connection.koomyo_connection import (
			connection_error_message,
		)

		return finish(connection, summary, preview, error=connection_error_message(e))

	frappe.db.savepoint("koomyo_catalog")
	for record in menu:
		guarded(summary, "menu", record, lambda r=record: load_menu_item(connection, client, r, categories))
	for record in ingredients:
		guarded(summary, "ingredients", record, lambda r=record: load_ingredient(connection, client, r))
	if preview:
		frappe.db.rollback(save_point="koomyo_catalog")
	return finish(connection, summary, preview)


def guarded(summary, kind, record, fn):
	"""One record at a time: a problem with one item is reported and the rest still load."""
	frappe.db.savepoint("koomyo_catalog_item")
	while True:
		try:
			summary[kind][fn()] += 1
			return
		except KoomyoAPIError as e:
			if e.code == "rate_limited":
				SLEEP((e.retry_after or 5) + 1)  # slow and steady, then the same record again
				continue
			error = str(e)
		except (NeedsMapping, Fatal, frappe.ValidationError) as e:
			error = frappe.utils.strip_html(str(e))
		frappe.db.rollback(save_point="koomyo_catalog_item")
		summary["problems"].append(f"{record.get('name') or record.get('id')}: {error}"[:300])
		frappe.local.message_log = []
		return


def read_list(client, path, params) -> list[dict]:
	rows, cursor = [], None
	while True:
		while True:
			try:
				page = client.get(path, {**params, **({"cursor": cursor} if cursor else {})})
				break
			except KoomyoAPIError as e:
				if e.code != "rate_limited":
					raise
				SLEEP((e.retry_after or 5) + 1)
		check_list_page(page, cursor_field="next_cursor")
		rows += page["data"]
		cursor = page["next_cursor"] if page["has_more"] else None
		if not cursor:
			return rows


# ------------------------------------------------------------------ menu items


def load_menu_item(connection, client, record: dict, categories: dict) -> str:
	if record.get("object") != "menu_item" or not record.get("id"):
		raise Fatal(_("Koomyo sent something that is not a menu item."))
	name = (record.get("name") or record["id"])[:140]
	group = category_group(connection, record, categories)
	description = menu_description(record)
	disabled = 0 if record.get("active", True) else 1

	existing = find_existing("Item", connection, record["id"])
	if existing:
		item = frappe.get_doc("Item", existing)
		changes = {"item_name": name, "item_group": group, "description": description, "disabled": disabled}
		if all((item.get(k) or "") == (v or "") for k, v in changes.items()):
			outcome = "unchanged"
		else:
			item.update(changes)
			item.save(ignore_permissions=True)
			outcome = "updated"
	else:
		code = get_or_create_item(
			connection, record["id"], name, is_stock_item=0, item_group=group, description=description
		)
		item = frappe.get_doc("Item", code)
		if disabled:
			item.db_set("disabled", 1)
		outcome = "created"

	set_price(connection, item.name, record.get("base_price"))
	write_reference(connection, client, "menu_item", record, item)
	return outcome


def category_group(connection, record: dict, categories: dict) -> str:
	"""A leaf Item Group per Koomyo category, under one '<connection> Menu' group."""
	category = categories.get(record.get("category_id") or "") or {}
	category_name = category.get("name") or record.get("category")
	if not category_name:
		return setting(connection, "menu_item_group", _("Menu Item Group"))
	parent = f"{connection.name} Menu"[:140]
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


def menu_description(record: dict) -> str:
	"""Plain facts from Koomyo, escaped: Koomyo text is data, never markup."""
	parts = []
	if record.get("description"):
		parts.append(escape_html(record["description"]))
	sizes = [s for s in record.get("sizes") or [] if s.get("active", True)]
	if sizes:
		parts.append(
			_("Sizes: {0}").format(
				", ".join(
					escape_html(
						f"{s.get('name')} {s.get('price')}" if s.get("price") else s.get("name") or ""
					)
					for s in sizes
				)
			)
		)
	if record.get("station"):
		parts.append(_("Station: {0}").format(escape_html(record["station"])))
	return "<br>".join(parts) or escape_html(record.get("name") or "")


def set_price(connection, item_code: str, base_price):
	"""The base price on the selling price list, for reference. Invoices always use Koomyo's line price."""
	if not connection.price_list or base_price in (None, ""):
		return
	price = flt(str(money(base_price, "base_price")), 2)
	existing = frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": connection.price_list},
		["name", "price_list_rate"],
		as_dict=True,
	)
	if existing:
		if flt(existing.price_list_rate, 2) != price:
			frappe.db.set_value("Item Price", existing.name, "price_list_rate", price)
		return
	frappe.get_doc(
		{
			"doctype": "Item Price",
			"item_code": item_code,
			"price_list": connection.price_list,
			"price_list_rate": price,
		}
	).insert(ignore_permissions=True)


# ------------------------------------------------------------------ ingredients


def load_ingredient(connection, client, record: dict) -> str:
	if record.get("object") != "ingredient" or not record.get("id"):
		raise Fatal(_("Koomyo sent something that is not an ingredient."))
	existing = find_existing("Item", connection, record["id"])
	if not existing:
		ingredient_item(connection, {"ingredient_id": record["id"]}, client=client, ingredient=record)
		return "created"
	item = frappe.get_doc("Item", existing)
	changes = {
		"item_name": (record.get("name") or record["id"])[:140],
		"disabled": 0 if record.get("active", True) else 1,
	}
	if all(item.get(k) == v for k, v in changes.items()):
		return "unchanged"
	item.update(changes)
	item.save(ignore_permissions=True)
	return "updated"


# ------------------------------------------------------------------ summary


def finish(connection, summary: dict, preview: bool, error: str | None = None) -> dict:
	m, i = summary["menu"], summary["ingredients"]
	verb = _("Preview: would load") if preview else _("Loaded")
	text = _(
		"{0} menu items ({1} new, {2} changed, {3} as they were) and ingredients ({4} new, {5} changed, {6} as they were)."
	).format(verb, m["created"], m["updated"], m["unchanged"], i["created"], i["updated"], i["unchanged"])
	if summary["problems"]:
		text += (
			" "
			+ _("{0} could not be loaded:").format(len(summary["problems"]))
			+ "\n"
			+ "\n".join(summary["problems"][:20])
		)
	if error:
		text = error
	summary["text"] = text
	frappe.db.set_value(
		"Koomyo Connection",
		connection.name,
		{"catalog_summary": text[:2000], "catalog_loaded_on": now_datetime()},
		update_modified=False,
	)
	frappe.db.commit()
	return summary
