"""Menu and ingredient events keep the Items up to date as they change in Koomyo.

Docs: events/catalog (menu_item.created / updated / deleted, ingredient.created / updated). Each event carries the
whole record, so the same code that "Load Menu and Ingredients" runs for every record handles it here:

- an ingredient that has no Item yet gets one (its unit and pack from the UOM Map); one that has an Item is updated
  (name, active, kind) and its unit and pack follow Koomyo's (sync/item_units.py: aligned when the Item has no
  transactions, flagged once for a person when it has);
- a menu item is created or updated with its category, sizes (one variant per size) and price;
- a deleted menu item is disabled with its size variants, never deleted: invoices already booked still point at it.

A record that needs a mapping (an ingredient unit not on the UOM Map, no Menu or Ingredient Item Group) fails as
NeedsMapping naming it, and books on Retry once it is set. A repeat of an event changes nothing.
"""

import frappe
from frappe import _

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import Fatal, Skipped
from koomyo_erpnext.sync.catalog import load_ingredient, load_menu_item
from koomyo_erpnext.sync.common import find_existing
from koomyo_erpnext.sync.events import handles

INGREDIENT_SCOPES = ("stock.read", "refs.write")
MENU_SCOPES = ("catalog.read", "refs.write")


@handles("ingredient.created", "ingredient.updated", switch="sync_stock", scopes=INGREDIENT_SCOPES)
def on_ingredient(event: dict, connection):
	ingredient = event["data"]
	if ingredient.get("object") != "ingredient" or not ingredient.get("id"):
		raise Fatal(_("Koomyo sent something that is not an ingredient."))
	load_ingredient(connection, KoomyoClient.for_connection(connection), ingredient)
	return ("Item", find_existing("Item", connection, ingredient["id"]))


@handles("menu_item.created", "menu_item.updated", switch="sync_sales", scopes=MENU_SCOPES)
def on_menu_item(event: dict, connection):
	record = event["data"]
	if record.get("object") != "menu_item" or not record.get("id"):
		raise Fatal(_("Koomyo sent something that is not a menu item."))
	# The record names its category, so the category list is not read again for one menu item.
	load_menu_item(connection, KoomyoClient.for_connection(connection), record, {})
	return ("Item", find_existing("Item", connection, record["id"]))


@handles("menu_item.deleted", switch="sync_sales", scopes=MENU_SCOPES)
def on_menu_item_deleted(event: dict, connection):
	record = event["data"]
	if record.get("object") != "menu_item" or not record.get("id"):
		raise Fatal(_("Koomyo sent something that is not a menu item."))
	code = find_existing("Item", connection, record["id"])
	if not code:
		raise Skipped(
			_("Menu item {0} was never made an Item here, so there is nothing to disable.").format(
				record.get("name") or record["id"]
			)
		)
	variants = frappe.get_all(
		"Item", filters={"variant_of": code, "koomyo_connection": connection.name}, pluck="name"
	)
	names = [code, *variants]
	changed = [n for n in names if not frappe.db.get_value("Item", n, "disabled")]
	for name in changed:
		frappe.db.set_value("Item", name, "disabled", 1)
	if changed:
		frappe.get_doc("Item", code).add_comment(
			"Comment", _("Deleted in Koomyo: the Item and its sizes are disabled, not deleted.")
		)
	return ("Item", code)
