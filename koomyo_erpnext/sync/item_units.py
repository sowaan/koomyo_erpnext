"""Keep an Item's unit and pack size the same as its Koomyo ingredient's.

Koomyo is the source of truth for ingredients. The stock UOM of an Item was set only when the Item was made, so
a unit changed in Koomyo (Straw: Pack to pcs, with a purchase unit of pack = 100) never reached ERPNext. One
function, `align_item`, does it for every path: an `ingredient.created` / `ingredient.updated` event, the menu
and ingredients load (the catalog backfill), and `heal_connection` (run once by a patch, so `bench migrate` heals
the Items of every connection by itself, and safe to run again).

The rules, per Item that belongs to the connection and carries the ingredient's Koomyo id:

- The Koomyo unit is read through the UOM Map. A unit that is not on it is `NeedsMapping` (with Retry).
- Stock UOM differs and the Item has **no transactions**: the stock UOM is set to match, the UOM rows are rebuilt
  (stock UOM at 1, then the pack from `purchase_unit` and `purchase_unit_factor` when that maps), and the pack is
  the Purchase UOM when none is set. This is ERPNext's own way (editing the Item and saving it, which keeps its
  own checks: ERPNext refuses a change once there is a Bin or stock entry).
- Stock UOM differs and the Item **has transactions** (a stock ledger entry, a Bin, an order, a receipt, a stock
  entry, a BOM, any document that names it): nothing is changed, and one Event Log entry that needs attention names
  the Item, the two units and what blocks it. It is raised once for that Item and pair of units, never again.
- Stock UOM matches: the pack row is added when missing (never blocked by transactions: it changes no stock). A pack
  size that changed is updated only on an Item with no transactions; otherwise the conversion is left and noted on
  the Item.
- A repeat changes nothing. One Item never fails the run of the others. Items of another connection, or with no
  Koomyo id, are never touched. Quantities and prices stay in Koomyo's own unit.

`dry_run` lists what would change and writes nothing.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import Fatal, KoomyoAPIError, NeedsMapping, Skipped
from koomyo_erpnext.sync.common import find_existing, money
from koomyo_erpnext.sync.events import EVENT_LOG, handles
from koomyo_erpnext.sync.stock import uom_for

EVENT_TYPE = "koomyo.item_unit"
SCOPES = ("stock.read",)

# (doctype, field naming the Item): any document row that names the Item is a transaction on it.
TRANSACTIONS = (
	("Stock Ledger Entry", "item_code"),
	("Bin", "item_code"),
	("Stock Entry Detail", "item_code"),
	("Stock Reconciliation Item", "item_code"),
	("Stock Reservation Entry", "item_code"),
	("Purchase Order Item", "item_code"),
	("Purchase Receipt Item", "item_code"),
	("Purchase Invoice Item", "item_code"),
	("Material Request Item", "item_code"),
	("Supplier Quotation Item", "item_code"),
	("Request for Quotation Item", "item_code"),
	("Sales Invoice Item", "item_code"),
	("POS Invoice Item", "item_code"),
	("Sales Order Item", "item_code"),
	("Delivery Note Item", "item_code"),
	("Quotation Item", "item_code"),
	("Packed Item", "item_code"),
	("Landed Cost Item", "item_code"),
	("BOM Item", "item_code"),
	("BOM Explosion Item", "item_code"),
	("BOM", "item"),
	("Work Order", "production_item"),
	("Work Order Item", "item_code"),
	("Subcontracting Order Item", "item_code"),
	("Subcontracting Receipt Item", "item_code"),
	("Batch", "item"),
	("Serial No", "item_code"),
)


def transactions_of(item_code: str) -> list[str]:
	"""What stops the stock UOM of this Item from changing: 'Stock Ledger Entry (3)', ... (empty when nothing does)."""
	found = []
	for doctype, field in TRANSACTIONS:
		try:
			count = frappe.db.count(doctype, {field: item_code})
		except Exception:  # a doctype this version or install does not have
			continue
		if count:
			found.append(f"{_(doctype)} ({count})")
	return found


def pack_of(connection, ingredient: dict, stock_uom: str) -> tuple[str, float] | None:
	"""(UOM, factor) of the ingredient's `purchase_unit` when it maps and is a real pack; else None (a hint only)."""
	unit, factor = ingredient.get("purchase_unit"), ingredient.get("purchase_unit_factor")
	if not unit or factor in (None, ""):
		return None
	try:
		uom = uom_for(connection, unit)
		wanted = flt(str(money(factor, "purchase_unit_factor")), 6)
	except (NeedsMapping, Fatal):
		return None
	if wanted <= 0 or uom == stock_uom:
		return None
	return uom, wanted


def describe(item) -> str:
	rows = ", ".join(f"{r.uom} = {flt(r.conversion_factor)}" for r in item.get("uoms") or [])
	return f"stock UOM {item.stock_uom}; UOMs [{rows}]; purchase UOM {item.get('purchase_uom') or '-'}"


def align_item(connection, item_name: str, ingredient: dict, dry_run: bool = False) -> dict:
	"""Align one Item to its Koomyo ingredient. Returns what was (or, in dry run, would be) done.

	`status`: unchanged | aligned | pack | blocked | skipped. Raises NeedsMapping for a unit that is not on the UOM Map.
	"""
	item = frappe.get_doc("Item", item_name)
	result = {
		"item": item.name,
		"item_name": item.item_name,
		"ingredient": ingredient.get("name"),
		"koomyo_unit": ingredient.get("unit"),
		"stock_uom": item.stock_uom,
		"status": "unchanged",
		"changes": [],
		"blockers": [],
	}
	if (
		item.get("koomyo_connection") != connection.name
		or not item.get("koomyo_id")
		or item.koomyo_id != ingredient.get("id")
	):
		result["status"] = "skipped"
		return result
	wanted = uom_for(connection, ingredient.get("unit"))  # NeedsMapping when the unit is not mapped
	pack = pack_of(connection, ingredient, wanted)
	before = describe(item)
	if item.stock_uom != wanted:
		blockers = transactions_of(item.name)
		if blockers:
			result.update(status="blocked", blockers=blockers, wanted_uom=wanted)
			return result
		rows = [{"uom": wanted, "conversion_factor": 1}]
		if pack:
			rows.append({"uom": pack[0], "conversion_factor": pack[1]})
		purchase = item.get("purchase_uom")
		purchase = purchase if purchase in {r["uom"] for r in rows} else (pack[0] if pack else None)
		result["changes"].append(
			_("stock UOM {0} to {1}; UOMs rebuilt: {2}").format(
				item.stock_uom, wanted, ", ".join(f"{r['uom']} = {r['conversion_factor']}" for r in rows)
			)
		)
		result["status"] = "aligned"
		if dry_run:
			return result
		try:
			# ERPNext clears the UOM table whenever the stock UOM changes ("redefine conversion factors"): save the
			# unit first, then put the rows back in a second save.
			item.stock_uom = wanted
			item.purchase_uom = wanted
			item.save(ignore_permissions=True)
			item.reload()
			item.set("uoms", rows)
			item.purchase_uom = purchase
			item.save(ignore_permissions=True)
		except frappe.ValidationError as e:
			# ERPNext's own guard said no (something the list above does not know): treat it as a transaction.
			frappe.db.rollback(save_point="koomyo_item_unit")
			item.reload()
			result.update(
				status="blocked",
				blockers=[frappe.utils.strip_html(str(e))[:300]],
				wanted_uom=wanted,
				changes=[],
			)
			return result
		record_change(item, before)
		return result
	if pack:
		uom, factor = pack
		row = next((r for r in item.get("uoms") or [] if r.uom == uom), None)
		if not row:
			result["changes"].append(_("added {0} = {1}").format(uom, factor))
			result["status"] = "pack"
			if not dry_run:
				item.append("uoms", {"uom": uom, "conversion_factor": factor})
				if not item.get("purchase_uom"):
					item.purchase_uom = uom
				item.save(ignore_permissions=True)
		elif abs(flt(row.conversion_factor) - factor) > 1e-6:
			if transactions_of(item.name):
				result["changes"].append(
					_("{0} kept at {1} (Koomyo says {2}); the Item has transactions").format(
						uom, flt(row.conversion_factor), factor
					)
				)
				result["status"] = "unchanged"
				if not dry_run:
					note_pack_size(item, uom, factor, row.conversion_factor)
			else:
				result["changes"].append(
					_("{0} = {1} to {2}").format(uom, flt(row.conversion_factor), factor)
				)
				result["status"] = "pack"
				if not dry_run:
					row.conversion_factor = factor
					item.save(ignore_permissions=True)
		if result["status"] == "pack" and not dry_run:
			record_change(item, before)
	return result


def record_change(item, before: str):
	"""Every change is written on the Item, with before and after."""
	item.reload()
	item.add_comment(
		"Comment",
		_("Unit aligned to Koomyo. Before: {0}. After: {1}.").format(before, describe(item)),
	)


def note_pack_size(item, uom: str, factor: float, held: float):
	note = _(
		"Koomyo now says {0} holds {1} {2}, but this Item holds {3}. The conversion was left as it is, because the Item has transactions."
	).format(uom, factor, item.stock_uom, flt(held))
	if not frappe.db.exists(
		"Comment", {"reference_doctype": "Item", "reference_name": item.name, "content": note}
	):
		item.add_comment("Comment", note)


# ------------------------------------------------------------------ the entry that needs attention


def attention_id(item_name: str, koomyo_unit: str, stock_uom: str) -> str:
	return f"unit-attention:{item_name}:{koomyo_unit}:{stock_uom}"


def blocked_message(result: dict, ingredient: dict) -> str:
	return _(
		"Item {0} ({1}) has stock UOM {2}, but Koomyo stocks ingredient {1} in {3} ({4}). The Item has transactions, so its unit was not changed: {5}. Move or cancel them (or make a new Item), then press Retry."
	).format(
		result["item"],
		result.get("ingredient") or result["item_name"],
		result["stock_uom"],
		ingredient.get("unit"),
		result.get("wanted_uom"),
		", ".join(result["blockers"]),
	)[:1000]


def already_flagged(connection, result: dict, ingredient: dict) -> bool:
	"""True when this Item and these two units already have an attention entry (from an event, a backfill or the patch)."""
	event_id = attention_id(result["item"], ingredient.get("unit") or "-", result["stock_uom"])
	return bool(
		frappe.db.exists(EVENT_LOG, {"connection": connection.name, "event_id": event_id})
		or frappe.db.exists(
			EVENT_LOG,
			{
				"connection": connection.name,
				"error_class": "Fatal",
				"message": blocked_message(result, ingredient),
			},
		)
	)


def flag_blocked(connection, result: dict, ingredient: dict) -> bool:
	"""One Event Log entry that needs attention for this Item and these two units. True when it was raised now."""
	if already_flagged(connection, result, ingredient):
		return False
	event_id = attention_id(result["item"], ingredient.get("unit") or "-", result["stock_uom"])
	message = blocked_message(result, ingredient)
	log = frappe.new_doc(EVENT_LOG)
	log.update(
		{
			"connection": connection.name,
			"company": connection.company,
			"event_id": event_id,
			"event_type": EVENT_TYPE,
			"seq": 0,
			"received_via": "ERPNext",
			"event_created": now_datetime(),
			"entity_type": "Item",
			"entity_id": result["item"],
			"payload": json.dumps(
				{"type": EVENT_TYPE, "data": {"id": ingredient.get("id"), "item": result["item"]}}, indent=1
			),
			"status": "Failed",
			"error_class": "Fatal",
			"message": message[:1000],
			"attempts": 0,
			"processed_on": now_datetime(),
		}
	)
	log.insert(ignore_permissions=True)
	return True


def flag_unmapped(connection, item_name: str, ingredient: dict, error) -> bool:
	"""A NeedsMapping entry (with Retry) for an ingredient whose unit is not on the UOM Map, once per unit."""
	event_id = f"unit-unmapped:{item_name}:{ingredient.get('unit')}"
	if frappe.db.exists(EVENT_LOG, {"connection": connection.name, "event_id": event_id}):
		return False
	log = frappe.new_doc(EVENT_LOG)
	log.update(
		{
			"connection": connection.name,
			"company": connection.company,
			"event_id": event_id,
			"event_type": EVENT_TYPE,
			"seq": 0,
			"received_via": "ERPNext",
			"event_created": now_datetime(),
			"entity_type": "Item",
			"entity_id": item_name,
			"payload": json.dumps(
				{"type": EVENT_TYPE, "data": {"id": ingredient.get("id"), "item": item_name}}, indent=1
			),
			"status": "Failed",
			"error_class": "NeedsMapping",
			"message": str(error)[:1000],
			"attempts": 0,
			"processed_on": now_datetime(),
		}
	)
	log.insert(ignore_permissions=True)
	return True


# ------------------------------------------------------------------ the events


def align_for_event(connection, ingredient: dict):
	"""Used by the events and by Retry: align the Item of this ingredient, or say why not."""
	if ingredient.get("object") != "ingredient" or not ingredient.get("id"):
		raise Fatal(_("Koomyo sent something that is not an ingredient."))
	name = find_existing("Item", connection, ingredient["id"])
	if not name:
		raise Skipped(
			_(
				"Ingredient {0} has no Item yet: it is made when it is first used, or by the menu and ingredients load."
			).format(ingredient.get("name") or ingredient["id"])
		)
	frappe.db.savepoint("koomyo_item_unit")
	result = align_item(connection, name, ingredient)
	if result["status"] == "blocked":
		# The event's own Event Log row is the attention entry (a separate row would be rolled back with the failure).
		if not already_flagged(connection, result, ingredient):
			raise Fatal(blocked_message(result, ingredient))
		raise Skipped(
			_("Item {0} was already flagged: its unit differs from Koomyo's and it has transactions.").format(
				name
			)
		)
	return ("Item", name)


@handles("ingredient.created", "ingredient.updated", switch="sync_stock", scopes=SCOPES)
def on_ingredient(event: dict, connection):
	return align_for_event(connection, event["data"])


@handles(EVENT_TYPE, switch="sync_stock", scopes=SCOPES)
def on_item_unit_retry(event: dict, connection, client: KoomyoClient | None = None):
	"""Retry of a flagged Item or an unmapped unit: read the ingredient now and align again."""
	ingredient_id = (event.get("data") or {}).get("id")
	client = client or KoomyoClient.for_connection(connection)
	ingredient = client.get(f"/ingredients/{ingredient_id}")
	return align_for_event(connection, ingredient)


# ------------------------------------------------------------------ every Item of a connection


def heal_connection(connection_name: str, dry_run: bool = False, client: KoomyoClient | None = None) -> dict:
	"""Align every Item of the connection to its Koomyo ingredient (what the patch queues; safe to run again).

	Reads the ingredients, never writes to Koomyo. One Item at a time: a problem with one is reported and the rest go on.
	"""
	connection = frappe.get_doc("Koomyo Connection", connection_name)
	summary = {
		"connection": connection.name,
		"dry_run": bool(dry_run),
		"read": 0,
		"items": [],
		"aligned": 0,
		"pack": 0,
		"blocked": 0,
		"unmapped": 0,
		"errors": [],
	}
	if not (connection.enabled and connection.sync_stock):
		summary["note"] = _("The connection is off or Sync Stock is off: nothing to align.")
		return summary
	client = client or KoomyoClient.for_connection(connection)
	try:
		ingredients = list(client.iter_list("/ingredients", {"limit": 500}))
	except KoomyoAPIError as e:
		summary["errors"].append(str(e)[:300])
		return summary
	for ingredient in ingredients:
		summary["read"] += 1
		name = find_existing("Item", connection, ingredient.get("id") or "")
		if not name:
			continue
		frappe.db.savepoint("koomyo_item_unit")
		try:
			result = align_item(connection, name, ingredient, dry_run=dry_run)
		except NeedsMapping as e:
			frappe.db.rollback(save_point="koomyo_item_unit")
			summary["unmapped"] += 1
			summary["items"].append(
				{
					"item": name,
					"ingredient": ingredient.get("name"),
					"status": "unmapped",
					"changes": [],
					"blockers": [str(e)],
				}
			)
			if not dry_run:
				flag_unmapped(connection, name, ingredient, e)
			continue
		except Exception as e:
			frappe.db.rollback(save_point="koomyo_item_unit")
			summary["errors"].append(f"{name}: {frappe.utils.strip_html(str(e))[:200]}")
			frappe.log_error(title=f"Koomyo item unit {name}")
			continue
		if result["status"] == "blocked" and not dry_run:
			flag_blocked(connection, result, ingredient)
		if result["status"] in ("aligned", "pack", "blocked"):
			summary["items"].append(result)
			summary["blocked" if result["status"] == "blocked" else result["status"]] += 1
	if not dry_run:
		frappe.db.commit()
	return summary


def report(connection_name: str, dry_run: int = 1):
	"""Print what heal_connection would do (or did): for `bench execute` on a site."""
	summary = heal_connection(connection_name, dry_run=bool(int(dry_run)))
	mode = "DRY RUN (nothing written)" if summary["dry_run"] else "DONE"
	print(f"{mode}: connection {summary['connection']}, {summary['read']} ingredients read")
	print(
		f"  to align {summary['aligned']}, pack rows {summary['pack']}, blocked {summary['blocked']}, "
		f"unmapped {summary['unmapped']}, errors {len(summary['errors'])}"
	)
	for r in summary["items"]:
		print(f"- {r['item']} {r.get('item_name') or ''} / {r.get('ingredient')}: {r['status']}")
		for text in r.get("changes") or []:
			print(f"    change: {text}")
		for text in r.get("blockers") or []:
			print(f"    blocked by: {text}")
	for text in summary["errors"]:
		print(f"  error: {text}")
	if summary.get("note"):
		print(summary["note"])


def queue_heal(connection_name: str):
	"""Queue the healing of one connection in the background (the patch uses this: Koomyo is read there, not in migrate)."""
	frappe.enqueue(
		"koomyo_erpnext.sync.item_units.heal_connection",
		queue="long",
		timeout=3600,
		job_id=f"koomyo-item-units-{frappe.local.site}-{connection_name}",
		deduplicate=True,
		enqueue_after_commit=True,
		connection_name=connection_name,
	)
