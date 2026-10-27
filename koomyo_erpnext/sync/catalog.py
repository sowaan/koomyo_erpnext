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
from koomyo_erpnext.sync.items import category_group, get_or_create_item, menu_template, size_variant
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

	read_options(client, summary)
	frappe.db.savepoint("koomyo_catalog")
	for record in menu:
		guarded(
			summary,
			"menu",
			record,
			lambda r=record: load_menu_item(connection, client, r, categories, summary),
		)
	for record in ingredients:
		guarded(summary, "ingredients", record, lambda r=record: load_ingredient(connection, client, r))
	if preview:
		frappe.db.rollback(save_point="koomyo_catalog")
	return finish(connection, summary, preview)


def read_options(client, summary: dict):
	"""Read the option groups (modifiers) for the summary: how many, and which recipes look wrong.

	Options are only read, for costing and bills of materials (sync/recipes.py): Koomyo deducts their
	stock itself, and no event says when one changes. A Koomyo without the endpoint is left alone.
	"""
	from koomyo_erpnext.sync.recipes import option_warnings

	try:
		groups = read_list(client, "/option-groups", {"limit": 200, "active": "true"})
	except KoomyoAPIError as e:
		if e.status != 404:
			summary["problems"].append(_("Options could not be read: {0}").format(e)[:300])
		return
	options = [o for g in groups for o in g.get("options") or []]
	summary["options"] = {
		"groups": len(groups),
		"options": len(options),
		"no_recipe": sum(1 for o in options if not o.get("recipe")),
		"warnings": [w for g in groups for w in option_warnings(g)],
	}


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


def load_menu_item(connection, client, record: dict, categories: dict, summary: dict | None = None) -> str:
	if record.get("object") != "menu_item" or not record.get("id"):
		raise Fatal(_("Koomyo sent something that is not a menu item."))
	name = (record.get("name") or record["id"])[:140]
	category = categories.get(record.get("category_id") or "") or {}
	group = category_group(connection, category.get("name") or record.get("category"))
	description = menu_description(record)
	active = bool(record.get("active", True))
	sizes = record.get("sizes") or []

	existed = find_existing("Item", connection, record["id"])
	code = None
	if record.get("has_sizes") and sizes:
		code = menu_template(connection, record["id"], name, group=group, description=description)
	if not code:
		code = existed or get_or_create_item(
			connection, record["id"], name, is_stock_item=0, item_group=group, description=description
		)
	outcome = update_item(
		code,
		{"item_name": name, "item_group": group, "description": description, "disabled": 0 if active else 1},
	)
	if not existed:
		outcome = "created"
	write_reference(connection, client, "menu_item", record, frappe.get_doc("Item", code))
	if frappe.db.get_value("Item", code, "has_variants"):
		# ERPNext prices the variants, never the template: each size carries its own price.
		load_sizes(connection, record, code, name, group, active, summary)
	else:
		set_price(connection, code, record.get("base_price"))
	return outcome


def load_sizes(
	connection, record: dict, template: str, name: str, group: str, active: bool, summary: dict | None
):
	"""One variant per Koomyo size, with its own price; sizes gone from Koomyo are disabled, never deleted."""
	counts = (summary if summary is not None else {}).setdefault(
		"variants", {"created": 0, "updated": 0, "unchanged": 0}
	)
	seen = set()
	for size in record.get("sizes") or []:
		if not size.get("id") or not size.get("name"):
			continue
		seen.add(size["id"])
		existed = find_existing("Item", connection, size["id"])
		code = size_variant(connection, record["id"], name, size["id"], size["name"], group=group)
		outcome = update_item(
			code,
			{
				"item_name": f"{name} ({size['name']})"[:140],
				"item_group": group,
				"disabled": 0 if (active and size.get("active", True)) else 1,
			},
		)
		counts["created" if not existed else outcome] += 1
		set_price(connection, code, size.get("price") or record.get("base_price"))
	for variant in frappe.get_all(
		"Item",
		filters={"variant_of": template, "koomyo_connection": connection.name, "disabled": 0},
		fields=["name", "koomyo_id"],
	):
		if variant.koomyo_id not in seen:
			frappe.db.set_value("Item", variant.name, "disabled", 1)
			counts["updated"] += 1


def update_item(code: str, changes: dict) -> str:
	item = frappe.get_doc("Item", code)
	if all((item.get(k) or "") == (v or "") for k, v in changes.items()):
		return "unchanged"
	item.update(changes)
	item.save(ignore_permissions=True)
	return "updated"


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
	v = summary.get("variants") or {"created": 0, "updated": 0, "unchanged": 0}
	verb = _("Preview: would load") if preview else _("Loaded")
	text = _(
		"{0} menu items ({1} new, {2} changed, {3} as they were), sizes as variants ({4} new, {5} changed, {6} as they were) and ingredients ({7} new, {8} changed, {9} as they were)."
	).format(
		verb,
		m["created"],
		m["updated"],
		m["unchanged"],
		v["created"],
		v["updated"],
		v["unchanged"],
		i["created"],
		i["updated"],
		i["unchanged"],
	)
	o = summary.get("options")
	if o:
		text += " " + _(
			"{0} option groups with {1} options were read ({2} take nothing from stock); they are only used for costing."
		).format(o["groups"], o["options"], o["no_recipe"])
		if o["warnings"]:
			text += " " + _("Recipes that count an ingredient twice:") + " " + "; ".join(o["warnings"][:5])
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
