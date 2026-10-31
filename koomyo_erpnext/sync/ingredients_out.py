"""ERPNext Items and Warehouses -> Koomyo ingredients and warehouses (scope `ingredients.write`).

Docs: OpenAPI POST/PATCH/DELETE /ingredients, PUT /ingredients/{id}/default-supplier and /low-stock-levels,
POST/PATCH /warehouses. Opt in per connection (**Send Items and Warehouses to Koomyo**, under Sync Stock).

Items (stock Items only):
- A new Item marked **Send To Koomyo As** (Food, Packaging, Supply) is made as an ingredient: name, unit (the
  UOM Map read backwards), kind and the Item's valuation rate as the opening cost. Opening stock is never sent
  (Koomyo: a stock adjustment, never a field). The Item then carries the Koomyo id.
- A linked Item's name, stock UOM and disabled flag are compared with Koomyo's ingredient and only what differs
  is patched (so an Item the menu load just updated sends nothing back). A unit cannot change once the
  ingredient is in use in Koomyo (409 `unit_in_use`): the row says so.
- The default supplier of the Item for the connection's Company becomes the ingredient's default supplier;
  clearing it removes the link (`null`). A Koomyo-side default is never removed by an Item that never had one.
- The Item's reorder levels, for warehouses on the Branch Map, become its low stock levels (a removed row
  switches the alert off there with 0).
- Deleting a linked Item calls `DELETE /ingredients/{id}`; when Koomyo answers 409 `in_use` the ingredient is
  switched off instead (`active` false), as the docs say.
Warehouses:
- A Warehouse marked **Send To Koomyo As A Warehouse** is created in Koomyo and added to the Branch Map as a
  Location Type Warehouse row. A Warehouse on the Branch Map that way has its name, address and disabled flag
  patched (409 `main_warehouse` and `warehouse_has_stock` say why not).
Cost changes stay in `sync/purchasing.py` (`push_item_cost`).
"""

from decimal import Decimal
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import flt

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import Fatal, KoomyoAPIError, NeedsMapping, Skipped
from koomyo_erpnext.sync.events import handles, idempotency_key, koomyo_write, store_outbound, warn
from koomyo_erpnext.sync.purchasing import push_supplier_doc, text
from koomyo_erpnext.sync.transfers_out import place_of

SCOPES = ("stock.read", "ingredients.write", "purchasing.read", "suppliers.write")
SWITCH = "push_ingredients"
KINDS = {"Food": "food", "Packaging": "packaging", "Supply": "supply"}
FOUR = Decimal("0.0001")


def connections_for(company: str | None = None) -> list:
	filters = {"enabled": 1, "sync_stock": 1, SWITCH: 1}
	if company:
		filters["company"] = company
	return [
		frappe.get_cached_doc("Koomyo Connection", n)
		for n in frappe.get_all("Koomyo Connection", filters=filters, pluck="name")
	]


def usable(connection_name: str | None):
	if not connection_name or not frappe.db.exists("Koomyo Connection", connection_name):
		return None
	connection = frappe.get_cached_doc("Koomyo Connection", connection_name)
	return connection if connection.enabled and connection.sync_stock and connection.get(SWITCH) else None


def call(connection, client, doc, method: str, path: str, body, action: str, event: dict) -> dict:
	return koomyo_write(
		connection,
		client,
		method,
		path,
		body,
		idempotency_key(doc.doctype, doc.name, doc.modified, f"{action}-{event.get('attempt', 0)}"),
	)


def ingredient_path(item_koomyo_id: str, tail: str = "") -> str:
	return f"/ingredients/{quote(item_koomyo_id, safe='')}{tail}"


# ------------------------------------------------------------------ Item doc_events


def default_supplier(item, company: str | None) -> str | None:
	rows = [r for r in item.get("item_defaults") or [] if r.get("default_supplier")]
	row = next((r for r in rows if r.company == company), None)
	return row.default_supplier if row else None


def reorder_warehouses(item) -> dict[str, float]:
	return {
		r.warehouse: flt(r.warehouse_reorder_level) for r in item.get("reorder_levels") or [] if r.warehouse
	}


def on_item_update(doc, method=None):
	if not doc.is_stock_item:
		return
	before = doc.get_doc_before_save()
	if doc.get("koomyo_id") and doc.get("koomyo_connection"):
		connection = usable(doc.koomyo_connection)
		if not connection or before is None:
			return
		extra = changes_of(before, doc, connection)
		if extra:
			store_outbound(connection, "erpnext.ingredient_update", doc, action="update", extra=extra)
		return
	kind = doc.get("koomyo_send_as")
	if not kind or kind not in KINDS:
		return
	connection = usable(doc.get("koomyo_target")) or sole_connection()
	if connection:
		store_outbound(connection, "erpnext.ingredient_create", doc, action="create")
	else:
		frappe.msgprint(
			_(
				"{0} is marked to be sent to Koomyo, but there is not exactly one connection with Send Items and Warehouses to Koomyo switched on. Choose one in Koomyo Connection, or switch the others off."
			).format(doc.name),
			indicator="orange",
		)


def sole_connection():
	found = connections_for()
	return found[0] if len(found) == 1 else None


def changes_of(before, doc, connection) -> dict:
	"""What changed on a linked Item that Koomyo has a place for (empty when nothing)."""
	extra: dict = {}
	if (
		before.item_name != doc.item_name
		or before.stock_uom != doc.stock_uom
		or before.disabled != doc.disabled
	):
		extra["fields"] = True
	was, now = default_supplier(before, connection.company), default_supplier(doc, connection.company)
	if was != now:
		extra["default_supplier"] = True
		extra["default_cleared"] = bool(was and not now)
	old_levels, new_levels = reorder_warehouses(before), reorder_warehouses(doc)
	if old_levels != new_levels:
		extra["levels"] = True
		extra["removed"] = sorted(set(old_levels) - set(new_levels))
	return extra


def on_item_trash(doc, method=None):
	if not (doc.get("koomyo_id") and doc.get("koomyo_connection")):
		return
	connection = usable(doc.koomyo_connection)
	if connection:
		store_outbound(
			connection,
			"erpnext.ingredient_delete",
			doc,
			action="delete",
			extra={"ingredient_id": doc.koomyo_id},
		)


# ------------------------------------------------------------------ create


def koomyo_unit(connection, uom: str) -> str:
	for row in connection.uom_map:
		if row.uom == uom:
			return row.koomyo_unit
	raise NeedsMapping(
		_("UOM {0} has no Koomyo unit. Add a UOM Map row for it on connection {1}, then press Retry.").format(
			uom, connection.name
		)
	)


@handles("erpnext.ingredient_create", switch=SWITCH, scopes=SCOPES)
def push_ingredient_create(event: dict, connection, client: KoomyoClient | None = None):
	item = frappe.get_doc("Item", event["data"]["name"])
	if item.get("koomyo_id"):
		raise Skipped(_("Item {0} is already a Koomyo ingredient.").format(item.name))
	kind = KINDS.get(item.get("koomyo_send_as") or "")
	if not kind or not item.is_stock_item:
		raise Skipped(_("Item {0} is not a stock Item marked to be sent to Koomyo.").format(item.name))
	cost = Decimal(str(flt(item.valuation_rate, 6)))
	body = {
		"name": item.item_name[:150],
		"unit": koomyo_unit(connection, item.stock_uom),
		"kind": kind,
		"cost_per_unit": text(max(cost, Decimal(0))),
		"active": not item.disabled,
		"external_ref": {"system": connection.ref_system or "erpnext", "external_id": item.name},
	}
	client = client or KoomyoClient.for_connection(connection)
	try:
		answer = call(connection, client, item, "POST", "/ingredients", body, "create", event)
	except KoomyoAPIError as e:
		if e.code == "name_in_use":
			raise Fatal(
				_(
					"Koomyo already has an ingredient named {0}. Rename the Item, or link it to that ingredient (set the Koomyo ingredient's external reference to the Item code)."
				).format(item.item_name)
			)
		if e.code == "feature_not_available":
			raise Fatal(
				_("Koomyo refused {0} as a supply: the Supplies switch is off for this restaurant.").format(
					item.name
				)
			)
		raise
	if answer.get("object") != "ingredient" or not answer.get("id"):
		raise Fatal(_("Koomyo answered the ingredient with something else."))
	item.db_set(
		{
			"koomyo_connection": connection.name,
			"koomyo_id": answer["id"],
			"koomyo_kind": answer.get("kind") or kind,
		},
		update_modified=False,
	)
	item.koomyo_connection, item.koomyo_id = connection.name, answer["id"]
	extras(connection, client, item, answer, {"default_supplier": True, "levels": True, "removed": []}, event)
	return ("Item", item.name)


# ------------------------------------------------------------------ update


@handles("erpnext.ingredient_update", switch=SWITCH, scopes=SCOPES)
def push_ingredient_update(event: dict, connection, client: KoomyoClient | None = None):
	item = frappe.get_doc("Item", event["data"]["name"])
	if item.get("koomyo_connection") != connection.name or not item.get("koomyo_id"):
		raise Skipped(_("Item {0} is not a Koomyo ingredient of this connection.").format(item.name))
	client = client or KoomyoClient.for_connection(connection)
	ingredient = client.get(ingredient_path(item.koomyo_id))
	if ingredient.get("object") != "ingredient" or ingredient.get("id") != item.koomyo_id:
		raise Fatal(_("Koomyo answered GET /ingredients/{0} with something else.").format(item.koomyo_id))
	sent = False
	if event["data"].get("fields"):
		sent = patch_fields(connection, client, item, ingredient, event) or sent
	if event["data"].get("default_supplier") or event["data"].get("levels"):
		sent = extras(connection, client, item, ingredient, event["data"], event) or sent
	if not sent:
		raise Skipped(_("Koomyo already holds what Item {0} says.").format(item.name))
	return ("Item", item.name)


def patch_fields(connection, client, item, ingredient: dict, event: dict) -> bool:
	body = {}
	if item.item_name[:150] != ingredient.get("name"):
		body["name"] = item.item_name[:150]
	unit = koomyo_unit(connection, item.stock_uom)
	if unit.strip().lower() != (ingredient.get("unit") or "").strip().lower():
		body["unit"] = unit
	if (not item.disabled) != bool(ingredient.get("active", True)):
		body["active"] = not item.disabled
	if not body:
		return False
	try:
		call(connection, client, item, "PATCH", ingredient_path(item.koomyo_id), body, "patch", event)
	except KoomyoAPIError as e:
		if e.code == "unit_in_use":
			raise Fatal(
				_(
					"Koomyo refused the new unit of Item {0}: the ingredient already has recipes, stock, movements or purchase lines counted in {1}, so its unit cannot change. Set the Stock UOM back, or make a new Item for the new unit."
				).format(item.name, ingredient.get("unit"))
			)
		if e.code == "name_in_use":
			raise Fatal(
				_("Koomyo already has an ingredient named {0}. Rename the Item.").format(item.item_name)
			)
		raise
	return True


def extras(connection, client, item, ingredient: dict, what: dict, event: dict) -> bool:
	sent = False
	if what.get("default_supplier"):
		sent = push_default_supplier(connection, client, item, ingredient, what, event) or sent
	if what.get("levels"):
		sent = push_low_levels(connection, client, item, what, event) or sent
	return sent


def push_default_supplier(connection, client, item, ingredient: dict, what: dict, event: dict) -> bool:
	name = default_supplier(item, connection.company)
	if not name:
		if not what.get("default_cleared") or not ingredient.get("default_supplier_id"):
			return False  # never remove a default ERPNext never set
		supplier_id = None
	else:
		supplier = frappe.get_doc("Supplier", name)
		if supplier.get("koomyo_connection") and supplier.koomyo_connection != connection.name:
			raise NeedsMapping(
				_("Supplier {0} belongs to connection {1}, not {2}.").format(
					name, supplier.koomyo_connection, connection.name
				)
			)
		supplier_id = supplier.get("koomyo_id") or push_supplier_doc(supplier, connection, client)
	if supplier_id == ingredient.get("default_supplier_id"):
		return False
	try:
		call(
			connection,
			client,
			item,
			"PUT",
			ingredient_path(item.koomyo_id, "/default-supplier"),
			{"supplier_id": supplier_id},
			"supplier",
			event,
		)
	except KoomyoAPIError as e:
		if e.code == "supplier_inactive":
			raise Fatal(
				_("Koomyo refused {0} as default supplier of {1}: it is switched off there.").format(
					name, item.name
				)
			)
		raise
	return True


def push_low_levels(connection, client, item, what: dict, event: dict) -> bool:
	levels = []
	for warehouse, level in reorder_warehouses(item).items():
		place = place_of(connection, warehouse)
		if place:
			levels.append({"place": {"type": place[0], "id": place[1]}, "level": text(level)})
	for warehouse in what.get("removed") or []:
		place = place_of(connection, warehouse)
		if place:
			levels.append({"place": {"type": place[0], "id": place[1]}, "level": "0"})
	if not levels:
		return False
	call(
		connection,
		client,
		item,
		"PUT",
		ingredient_path(item.koomyo_id, "/low-stock-levels"),
		{"levels": levels},
		"levels",
		event,
	)
	return True


# ------------------------------------------------------------------ delete


@handles("erpnext.ingredient_delete", switch=SWITCH, scopes=SCOPES)
def push_ingredient_delete(event: dict, connection, client: KoomyoClient | None = None):
	ingredient_id = event["data"].get("ingredient_id")
	if not ingredient_id:
		raise Fatal(_("The event has no ingredient id."))
	client = client or KoomyoClient.for_connection(connection)
	key = f"{event['data']['key']}-{event.get('attempt', 0)}"[:190]
	try:
		koomyo_write(connection, client, "DELETE", ingredient_path(ingredient_id), None, key)
	except KoomyoAPIError as e:
		if e.code == "not_found":
			raise Skipped(_("The ingredient is already gone from Koomyo."))
		if e.code == "in_use":
			koomyo_write(
				connection,
				client,
				"PATCH",
				ingredient_path(ingredient_id),
				{"active": False},
				key + "-off",
			)
			warn(
				_(
					"The ingredient is still used in Koomyo (a recipe, an order, stock history or a purchase), so it could not be deleted: it was switched off there instead."
				)
			)
			return ("Item", event["data"]["name"])
		raise
	return ("Item", event["data"]["name"])


# ------------------------------------------------------------------ Warehouse doc_events


def address_of(doc) -> str:
	return ", ".join(
		p
		for p in (doc.get("address_line_1"), doc.get("address_line_2"), doc.get("city"), doc.get("state"))
		if p
	)[:300]


def branch_row(connection, warehouse: str):
	return next(
		(r for r in connection.branch_map if r.warehouse == warehouse and r.location_type == "Warehouse"),
		None,
	)


def on_warehouse_update(doc, method=None):
	if doc.get("is_group"):
		return
	before = doc.get_doc_before_save()
	for connection in connections_for(doc.company):
		row = branch_row(connection, doc.name)
		if row:
			if before is None:
				return
			fields = [
				label
				for label, changed in (
					("name", before.warehouse_name != doc.warehouse_name),
					("address", address_of(before) != address_of(doc)),
					("active", bool(before.disabled) != bool(doc.disabled)),
				)
				if changed
			]
			if fields:
				store_outbound(
					connection, "erpnext.warehouse_update", doc, action="update", extra={"fields": fields}
				)
			return
	if doc.get("koomyo_send"):
		found = connections_for(doc.company)
		if found:
			store_outbound(found[0], "erpnext.warehouse_create", doc, action="create")


@handles("erpnext.warehouse_create", switch=SWITCH, scopes=SCOPES)
def push_warehouse_create(event: dict, connection, client: KoomyoClient | None = None):
	doc = frappe.get_doc("Warehouse", event["data"]["name"])
	if branch_row(connection, doc.name):
		raise Skipped(_("Warehouse {0} is already on the Branch Map as a Koomyo warehouse.").format(doc.name))
	if not doc.get("koomyo_send"):
		raise Skipped(_("Warehouse {0} is not marked to be sent to Koomyo.").format(doc.name))
	client = client or KoomyoClient.for_connection(connection)
	body = {
		"name": doc.warehouse_name[:100],
		"external_ref": {"system": connection.ref_system or "erpnext", "external_id": doc.name},
	}
	if address_of(doc):
		body["address"] = address_of(doc)
	try:
		answer = call(connection, client, doc, "POST", "/warehouses", body, "create", event)
	except KoomyoAPIError as e:
		if e.code == "name_in_use":
			raise Fatal(
				_("Koomyo already has a warehouse named {0}. Rename the Warehouse.").format(
					doc.warehouse_name
				)
			)
		if e.code == "place_not_allowed":
			raise Fatal(
				_(
					"Koomyo refused: warehouses belong to the whole restaurant and this key is limited to a branch. Use a key that is not limited to a branch."
				)
			)
		raise
	if not answer.get("id"):
		raise Fatal(_("Koomyo answered the warehouse with something else."))
	connection.reload()
	if not branch_row(connection, doc.name):
		connection.append(
			"branch_map",
			{
				"location_type": "Warehouse",
				"koomyo_branch_id": answer["id"],
				"koomyo_branch_name": answer.get("name") or doc.warehouse_name,
				"warehouse": doc.name,
				"cost_center": connection.get("default_cost_center"),
			},
		)
		connection.save(ignore_permissions=True)
	return ("Warehouse", doc.name)


@handles("erpnext.warehouse_update", switch=SWITCH, scopes=SCOPES)
def push_warehouse_update(event: dict, connection, client: KoomyoClient | None = None):
	doc = frappe.get_doc("Warehouse", event["data"]["name"])
	row = branch_row(connection, doc.name)
	if not row:
		raise Skipped(_("Warehouse {0} is not a Koomyo warehouse of this connection.").format(doc.name))
	fields = event["data"].get("fields") or []
	body = {}
	if "name" in fields:
		body["name"] = doc.warehouse_name[:100]
	if "address" in fields:
		body["address"] = address_of(doc)
	if "active" in fields:
		body["active"] = not doc.disabled
	if not body:
		raise Skipped(_("Nothing to change in Koomyo for Warehouse {0}.").format(doc.name))
	client = client or KoomyoClient.for_connection(connection)
	try:
		call(
			connection,
			client,
			doc,
			"PATCH",
			f"/warehouses/{quote(row.koomyo_branch_id, safe='')}",
			body,
			"patch",
			event,
		)
	except KoomyoAPIError as e:
		explain = {
			"main_warehouse": _("Koomyo's main warehouse can never be switched off."),
			"warehouse_has_stock": _(
				"Koomyo's warehouse still holds stock, so it cannot be switched off. Move the stock out first."
			),
			"name_in_use": _("Koomyo already has a warehouse of that name. Rename the Warehouse."),
			"place_not_allowed": _("This key is limited to a branch and cannot change warehouses."),
		}.get(e.code)
		if explain:
			raise Fatal(_("Koomyo refused the change of Warehouse {0}: {1}").format(doc.name, explain))
		raise
	return ("Warehouse", doc.name)
