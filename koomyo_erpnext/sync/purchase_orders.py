"""Koomyo purchase orders -> ERPNext Purchase Orders and Purchase Receipts.

Docs: docs.koomyo.com/events/purchasing, /guides/stock-purchasing, OpenAPI schemas PurchaseOrder,
Event_PurchaseCreated / Updated / Cancelled / Received, GET /purchase-orders?updated_since=.

One code path, `apply_order`, handles an order whatever brought it here: the four events
(`purchase.created|updated|cancelled|received`) or the periodic `updated_since` read
(`koomyo.purchase_sync`, queued as its own Event Log row so it is visible and retryable).
Each answer carries the whole order, so the state is rebuilt from it and replays change nothing.

- draft: a plan, not a commitment. Nothing is created.
- ordered / partial / received: a submitted Purchase Order (made here only when "Book Orders Made
  in Koomyo" is on; an order made in ERPNext is never touched, only receipted).
- an edit of a Koomyo-made order that nothing has arrived against: the Purchase Order is cancelled and
  made again from the new copy (a submitted order cannot be edited). After a delivery only the notes
  can change, and they are copied.
- cancelled: the Purchase Order is cancelled; if goods were already received in ERPNext the event
  fails with a message for a person, and nothing is forced.
- every delivery (`receipts[]`) is its own Purchase Receipt, keyed by the receipt id.

Money. The order's tax, withholding and extra charges become Purchase Taxes and Charges rows:
recoverable tax -> the Purchase Tax Account; tax that cannot be claimed back and charges spread over
cost -> the Landed Cost Account, inside the cost of the goods; withholding -> the Withholding Tax
Account as a deduction, never part of cost; other charges -> the Charges Expense Account. Charges
spread by quantity are posted with a Landed Cost Voucher on each receipt (ERPNext's own tax rows
only spread by value). A delivery carries its share of each row (by value or quantity delivered so
far); the last delivery takes the remainder, so the rows always add up to Koomyo's figures.
"""

import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import Fatal, NeedsMapping, Skipped
from koomyo_erpnext.sync.common import (
	branch_row,
	check_company,
	find_existing,
	local_date,
	money,
	posting_date,
	setting,
	stamp,
	to_field,
)
from koomyo_erpnext.sync.events import handles, read_only

SYNC_TYPE = "koomyo.purchase_sync"
EVENT_TYPES = ("purchase.created", "purchase.updated", "purchase.cancelled", "purchase.received", SYNC_TYPE)
SCOPES = ("purchasing.read",)
CENT = Decimal("0.01")
ZERO = Decimal(0)
# What changes the money or the goods; a change to anything else (notes, expected date, the send) is not rebuilt.
MATERIAL = ("supplier", "lines", "charges", "tax")


@handles(*EVENT_TYPES, switch="sync_purchasing", scopes=SCOPES)
def on_purchase_event(event: dict, connection):
	order = event["data"]
	if order.get("object") != "purchase_order" or not order.get("id"):
		raise Fatal(_("Koomyo sent something that is not a purchase order."))
	return apply_order(order, connection, event["type"])


# ------------------------------------------------------------------ the one path


def apply_order(order: dict, connection, event_type: str = SYNC_TYPE):
	status = order.get("status")
	if status not in ("draft", "ordered", "partial", "received", "cancelled"):
		raise Fatal(_("Koomyo sent purchase order {0} with status {1!r}.").format(order["id"], status))

	po = existing_order(order, connection)
	if po and is_stale(po, order):
		return ("Purchase Order", po.name)

	if status == "cancelled":
		return cancel_counterpart(po, order, connection)
	if status == "draft":
		raise Skipped(
			_(
				"Purchase order {0} is a draft: a plan, not a commitment. Nothing is created until it is ordered."
			).format(order.get("number") or order["id"])
		)

	if not po:
		if not connection.book_koomyo_purchases:
			raise Skipped(
				_(
					"Purchase order {0} was made in Koomyo, not in ERPNext. Switch on Book Orders Made in Koomyo to book such orders, or book its receipt by hand."
				).format(order["id"])
			)
		po = create_order(connection, order)
	elif origin(po, order, connection) == "koomyo":
		po = refresh_order(po, order, connection, event_type)
	else:
		note_seen(po, order)

	last = None
	for receipt in deliveries(order):
		booked = book_delivery(po, order, receipt, connection)
		last = booked or last
	return ("Purchase Receipt", last) if last else ("Purchase Order", po.name)


def existing_order(order: dict, connection):
	name = find_existing("Purchase Order", connection, order["id"])
	ref = (order.get("external_refs") or {}).get(connection.ref_system or "erpnext")
	if not name and ref and frappe.db.exists("Purchase Order", ref):
		name = ref
	if not name:
		return None
	po = frappe.get_doc("Purchase Order", name)
	if po.company != connection.company:
		raise NeedsMapping(
			_("Purchase Order {0} belongs to {1}, not to {2}.").format(
				po.name, po.company, connection.company
			)
		)
	return po


def origin(po, order: dict, connection) -> str:
	"""`koomyo` for an order this app made from Koomyo's copy, `erpnext` for one made in ERPNext."""
	return "koomyo" if po.get("koomyo_content") else "erpnext"


def is_stale(po, order: dict) -> bool:
	"""A copy older than the one already booked changes nothing (webhooks can arrive late)."""
	seen, new = po.get("koomyo_updated_at"), order.get("updated_at")
	if not seen or not new:
		return False
	return get_datetime(new) < get_datetime(seen)


def note_seen(po, order: dict):
	if order.get("updated_at"):
		po.db_set("koomyo_updated_at", order["updated_at"], update_modified=False)


# ------------------------------------------------------------------ building a Purchase Order


def exact(value, what: str) -> Decimal:
	return money(value, what)


def order_lines(order: dict) -> list[dict]:
	rows = []
	for line in order.get("lines") or []:
		ordered = exact(line.get("quantity_ordered"), "quantity_ordered")
		if ordered <= 0:
			continue
		tax = exact(line.get("tax_amount") or "0", "tax_amount")
		if line.get("line_total") is not None:
			net = exact(line["line_total"], "line_total") - tax
		else:  # an answer from before orders carried tax: the line is its cost
			net = ordered * exact(line.get("unit_cost"), "unit_cost")
		rows.append({"line": line, "qty": ordered, "net": net})
	return rows


def fingerprint(order: dict) -> str:
	"""A hash of everything that changes the Purchase Order's goods or money."""
	material = {
		"supplier": (order.get("supplier") or {}).get("id"),
		"branch": order.get("branch_id"),
		"warehouse": order.get("warehouse_id"),
		"lines": [
			[
				line.get("id"),
				line.get("ingredient_id"),
				line.get("quantity_ordered"),
				line.get("unit_cost"),
				line.get("line_total"),
				line.get("tax_amount"),
				[
					[a.get("id"), a.get("quantity"), a.get("destination_type"), a.get("destination_id")]
					for a in line.get("allocations") or []
				],
			]
			for line in order.get("lines") or []
		],
		"charges": [
			[c.get("id"), c.get("amount"), c.get("spread_over_cost"), c.get("role"), c.get("tax_amount")]
			for c in order.get("charges") or []
		],
		"tax": [order.get("tax_total"), order.get("withholding_total"), order.get("non_recoverable_tax")],
		"total": order.get("total_ordered"),
	}
	return hashlib.sha1(json.dumps(material, sort_keys=True).encode()).hexdigest()


def order_warehouse(connection, order: dict) -> str:
	"""Where the goods go: the branch, else the Koomyo warehouse the order names, else the main warehouse."""
	if order.get("branch_id"):
		warehouse = branch_row(connection, order["branch_id"]).warehouse
	elif order.get("warehouse_id"):
		from koomyo_erpnext.sync.stock import location_row

		warehouse = location_row(connection, "warehouse", order["warehouse_id"]).warehouse
	else:
		warehouse = setting(connection, "main_warehouse", _("Main Warehouse"))
	if not warehouse:
		raise NeedsMapping(_("The branch of purchase order {0} has no Warehouse.").format(order["id"]))
	check_company(connection, "Warehouse", warehouse, _("Purchase order {0}").format(order["id"]))
	return warehouse


def order_cost_center(connection, order: dict) -> str | None:
	"""The branch's Cost Center, else the connection's default, else the Company's: tax rows need one."""
	if order.get("branch_id"):
		center = branch_row(connection, order["branch_id"]).cost_center
		if center:
			return center
	return connection.default_cost_center or frappe.get_cached_value(
		"Company", connection.company, "cost_center"
	)


def charge_method(connection, charge: dict) -> str:
	"""none | value | quantity, from the connection's override or the charge's own setting."""
	override = {"None": "none", "By Value": "value", "By Quantity": "quantity"}.get(
		connection.landed_cost_method
	)
	return override or charge.get("spread_over_cost") or "none"


def account_for(connection, fieldname: str, label: str, why: str) -> str:
	account = connection.get(fieldname)
	if not account:
		raise NeedsMapping(
			_("Order {0}: set {1} on connection {2} (needed for {3}), then press Retry.").format(
				why.split("|")[0], label, connection.name, why.split("|")[1]
			)
		)
	check_company(connection, "Account", account, label)
	return account


def tax_rows(connection, order: dict, cost_center: str) -> list[dict]:
	"""The order's tax, charges and withholding as Actual rows of Purchase Taxes and Charges.

	Each row also carries `basis` (value | quantity | None): how a delivery's share is taken.
	"""
	number = order.get("number") or order["id"]
	rows = []

	def row(account, description, amount: Decimal, category: str, basis, add=True):
		if not amount:
			return
		rows.append(
			{
				"charge_type": "Actual",
				"account_head": account,
				"cost_center": cost_center,
				"description": description[:140],
				"tax_amount": to_field(abs(amount)),
				"add_deduct_tax": "Add" if add else "Deduct",
				"category": category,
				"basis": basis,
			}
		)

	for tax in order.get("tax_lines") or []:
		amount = exact(tax.get("tax"), "tax")
		if not amount:
			continue
		label = f"{tax.get('name') or _('Tax')} {tax.get('rate')}%"
		if tax.get("recoverable"):
			account = account_for(
				connection, "purchase_tax_account", _("Purchase Tax Account"), f"{number}|{_('input tax')}"
			)
			row(account, label, amount, "Total", "value")
		else:
			account = account_for(
				connection,
				"purchase_charge_account",
				_("Landed Cost Account"),
				f"{number}|{_('tax that cannot be claimed back')}",
			)
			row(account, label + " " + _("(not recoverable)"), amount, "Valuation and Total", "value")

	for charge in order.get("charges") or []:
		amount = exact(charge.get("amount"), "charge amount")
		if charge.get("role") == "withholding":
			account = account_for(
				connection,
				"purchase_withholding_account",
				_("Withholding Tax Account"),
				f"{number}|{_('withholding tax')}",
			)
			row(
				account, charge.get("label") or _("Withholding tax"), amount, "Total", "value", add=amount > 0
			)
			continue
		method = charge_method(connection, charge)
		label = charge.get("label") or _("Charge")
		if method == "none":
			account = account_for(
				connection,
				"purchase_charge_expense_account",
				_("Charges Expense Account"),
				f"{number}|{_('charges')}",
			)
			row(account, label, amount, "Total", "value", add=amount > 0)
		else:
			account = account_for(
				connection,
				"purchase_charge_account",
				_("Landed Cost Account"),
				f"{number}|{_('charges spread over cost')}",
			)
			# Quantity spread is posted by a Landed Cost Voucher on each receipt; the Purchase Order only carries the amount.
			category = "Valuation and Total" if method == "value" else "Total"
			row(account, label, amount, category, method, add=amount > 0)
	return rows


def create_order(connection, order: dict):
	"""A submitted Purchase Order (and its Supplier) for an order made in the Koomyo admin."""
	from koomyo_erpnext.sync.customers import write_reference
	from koomyo_erpnext.sync.purchasing import koomyo_supplier
	from koomyo_erpnext.sync.stock import ingredient_item

	shared = any(line.get("allocations") for line in order.get("lines") or [])
	every_line_shared = shared and all(line.get("allocations") for line in order.get("lines") or [])
	warehouse = None if every_line_shared else order_warehouse(connection, order)
	cost_center = order_cost_center(connection, order)
	client = KoomyoClient.for_connection(connection)
	po = frappe.new_doc("Purchase Order")
	date = str(posting_date(order))
	po.update(
		{
			"company": connection.company,
			"supplier": koomyo_supplier(connection, order.get("supplier") or {}, client),
			"transaction_date": date,
			"schedule_date": order.get("expected_date") or date,
			"set_warehouse": warehouse if not shared else None,
			"currency": order.get("currency") or connection.koomyo_currency,
			"conversion_rate": 1,
			"disable_rounded_total": 1,
			"remarks": _("Koomyo purchase order {0}").format(order.get("number") or order["id"]),
		}
	)
	if order.get("notes"):
		po.remarks += ". " + order["notes"][:300]
	stamp(po, connection, order["id"], order.get("branch_id"))
	lines = order_lines(order)
	for entry in lines:
		line = entry["line"]
		item_code = ingredient_item(connection, line, client=client)
		rate = to_field(entry["net"] / entry["qty"], 6)
		shares = [a for a in line.get("allocations") or [] if money(a.get("quantity") or "0", "quantity") > 0]
		# A line with branch shares is one row per share, each for its place: the branch's Warehouse, or the
		# warehouse the goods go to first.
		for share in shares or [None]:
			po.append(
				"items",
				{
					"item_code": item_code,
					"qty": to_field(money(share["quantity"], "quantity"), 6)
					if share
					else to_field(entry["qty"], 6),
					"rate": rate,
					"warehouse": share_warehouse(connection, share) if share else warehouse,
					"schedule_date": order.get("expected_date") or date,
				},
			)
	if not po.items:
		raise Skipped(_("Purchase order {0} has no lines.").format(order["id"]))
	for row in tax_rows(connection, order, cost_center or ""):
		row.pop("basis")
		po.append("taxes", row)
	po.insert(ignore_permissions=True)
	check_total(po, order)
	po.submit()
	po.db_set(
		{"koomyo_updated_at": order.get("updated_at"), "koomyo_content": fingerprint(order)},
		update_modified=False,
	)
	write_reference(connection, client, "purchase_order", order, po)
	return po


def share_warehouse(connection, share: dict) -> str:
	"""The ERPNext Warehouse of a share: its branch's, or the Koomyo warehouse it is delivered to."""
	from koomyo_erpnext.sync.stock import location_row

	if share.get("destination_type") == "warehouse":
		row = location_row(connection, "warehouse", share.get("destination_id"))
	else:
		row = branch_row(connection, share.get("destination_id") or share.get("branch_id"))
	if not row.warehouse:
		raise NeedsMapping(
			_("Koomyo place {0} has no Warehouse on the Branch Map.").format(share.get("destination_id"))
		)
	return row.warehouse


def receipt_warehouse(connection, receipt: dict) -> str | None:
	"""Where a delivery landed (`location_type` and `location_id` of the receipt); None on an older answer."""
	if not receipt.get("location_id"):
		return None
	from koomyo_erpnext.sync.stock import location_row

	return location_row(connection, receipt.get("location_type"), receipt["location_id"]).warehouse


def check_total(po, order: dict):
	"""ERPNext's total must be Koomyo's total_ordered; rounding of per-unit costs may differ by a few cents."""
	wanted = exact(order.get("total_ordered"), "total_ordered")
	got = Decimal(str(flt(po.grand_total, 2))).quantize(CENT)
	tolerance = CENT * (len(po.items) + len(po.taxes))
	if abs(got - wanted) > tolerance:
		raise Fatal(
			_(
				"Purchase order {0}: ERPNext computed {1} but Koomyo's total is {2}. Nothing was booked."
			).format(order.get("number") or order["id"], got, wanted)
		)
	if got != wanted:
		po.remarks = (
			(po.remarks or "")
			+ " | "
			+ _("Rounding: ERPNext total {0}, Koomyo total {1}.").format(got, wanted)
		)


# ------------------------------------------------------------------ edits and cancellation


def received_against(po) -> list[str]:
	return frappe.get_all(
		"Purchase Receipt Item",
		filters={"purchase_order": po.name, "docstatus": 1},
		pluck="parent",
		distinct=True,
	)


def refresh_order(po, order: dict, connection, event_type: str):
	"""A Koomyo-made order that Koomyo changed: rebuild it, or copy the notes once goods have arrived."""
	if po.docstatus == 2:
		raise Skipped(_("Purchase Order {0} was cancelled in ERPNext; it is left as it is.").format(po.name))
	same = po.get("koomyo_content") == fingerprint(order)
	if same:
		note_seen(po, order)
		return po
	received = received_against(po)
	if received:
		if order.get("notes") and order["notes"] not in (po.remarks or ""):
			po.db_set("remarks", ((po.remarks or "") + ". " + order["notes"])[:1000], update_modified=False)
		po.db_set("koomyo_updated_at", order.get("updated_at"), update_modified=False)
		# Goods have arrived, so the order cannot be rebuilt; the lines on file stay as they are.
		return po
	old = po.name
	po.flags.ignore_permissions = True  # the app cancels what Koomyo cancelled, whoever queued the job
	po.cancel()
	# The old document keeps its history but gives up the Koomyo id, so the new one can have it.
	po.db_set({"koomyo_connection": None, "koomyo_id": None}, update_modified=False)
	new = create_order(connection, order)
	frappe.get_doc("Purchase Order", new.name).add_comment(
		"Comment", _("Replaces {0}, which was cancelled because Koomyo changed the order.").format(old)
	)
	return new


def cancel_counterpart(po, order: dict, connection):
	if not po:
		raise Skipped(
			_("Purchase order {0} was cancelled in Koomyo; there is nothing in ERPNext to cancel.").format(
				order.get("number") or order["id"]
			)
		)
	if po.docstatus == 2:
		return ("Purchase Order", po.name)
	received = received_against(po)
	if received:
		raise Fatal(
			_(
				"Koomyo cancelled purchase order {0} ({1}) but goods were already received in ERPNext ({2}). Nothing was cancelled: decide by hand, then press Retry once the receipts are dealt with."
			).format(order.get("number") or order["id"], po.name, ", ".join(received))
		)
	po.flags.ignore_permissions = True  # the app cancels what Koomyo cancelled, whoever queued the job
	po.cancel()
	po.add_comment("Comment", _("Cancelled in Koomyo. Reason: {0}").format(order.get("cancel_reason") or "-"))
	return ("Purchase Order", po.name)


# ------------------------------------------------------------------ deliveries


def deliveries(order: dict) -> list[dict]:
	"""Every delivery of the order, oldest first.

	An order received before deliveries were kept (no `receipts`) is one delivery keyed by the order id.
	"""
	receipts = [r for r in order.get("receipts") or [] if r.get("id")]
	if receipts:
		return receipts
	if order.get("status") != "received" and not any(
		exact(l.get("quantity_received") or "0", "quantity_received") for l in order.get("lines") or []
	):
		return []
	return [
		{
			"id": order["id"],
			"received_at": order.get("received_at"),
			"lines": [
				{
					"line_id": l.get("id"),
					"ingredient_id": l.get("ingredient_id"),
					"quantity": l.get("quantity_received"),
				}
				for l in order.get("lines") or []
			],
			"legacy": True,
		}
	]


def share(total: Decimal, delivered: Decimal, ordered: Decimal, applied: Decimal, final: bool) -> Decimal:
	"""This delivery's part of `total`: by what has arrived so far, the last delivery taking the rest."""
	if final or not ordered:
		return total - applied
	cumulative = (total * min(delivered, ordered) / ordered).quantize(CENT, rounding=ROUND_HALF_UP)
	return cumulative - applied


def book_delivery(po, order: dict, receipt: dict, connection) -> str | None:
	"""The Purchase Receipt of one delivery, made once. Returns its name when it was made now."""
	receipt_id = receipt["id"]
	if receipt_booked(connection, receipt_id):
		return None
	if receipt.get("legacy") is None and find_existing("Purchase Receipt", connection, order["id"]):
		return None  # booked before deliveries were kept per receipt
	if origin(po, order, connection) == "erpnext" and link_unstamped_receipt(po, order, receipt, connection):
		return None
	if po.docstatus != 1:
		raise Skipped(_("Purchase Order {0} is not submitted.").format(po.name))

	from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

	quantities: dict[str, Decimal] = {}
	for line in receipt.get("lines") or []:
		quantities[line.get("ingredient_id")] = quantities.get(line.get("ingredient_id"), ZERO) + exact(
			line.get("quantity"), "quantity"
		)
	pr = make_purchase_receipt(po.name)
	place = receipt_warehouse(connection, receipt)
	if place:
		restrict_to_place(pr, po, place, order)
	# An ingredient on several rows (one per branch share) is filled row by row; the last row takes the rest.
	last_row = {}
	for item in pr.items:
		last_row[frappe.db.get_value("Item", item.item_code, "koomyo_id")] = id(item)
	delivered_value = ZERO
	for item in list(pr.items):
		ingredient_id = frappe.db.get_value("Item", item.item_code, "koomyo_id")
		factor = Decimal(str(flt(item.conversion_factor, 9) or 1))
		left = quantities.get(ingredient_id, ZERO)
		if not left:
			pr.remove(item)
			continue
		stock_qty = (
			left if id(item) == last_row[ingredient_id] else min(left, Decimal(str(item.qty)) * factor)
		)
		quantities[ingredient_id] = left - stock_qty
		item.qty = item.received_qty = to_field(stock_qty / factor, 6)
		delivered_value += stock_qty * Decimal(str(item.rate))
	if not pr.items:
		raise Skipped(_("Nothing was received on purchase order {0}.").format(order["id"]))
	pr.posting_date = local_date(receipt.get("received_at"), connection.koomyo_timezone) or posting_date(
		order
	)
	pr.set_posting_time = 1
	stamp(pr, connection, receipt_id, order.get("branch_id"))
	if receipt.get("delivery_note"):
		pr.supplier_delivery_note = str(receipt["delivery_note"])[:140]
	vouchers = apply_delivery_costs(pr, po, order, receipt, connection, delivered_value)
	pr.insert(ignore_permissions=True)
	pr.submit()
	for voucher in vouchers:
		make_landed_cost_voucher(pr, voucher, connection, receipt_id, order)
	return pr.name


def receipt_booked(connection, receipt_id: str) -> bool:
	"""This Koomyo delivery is a Purchase Receipt already: by its id, or among the other deliveries of a receipt
	that spans several places."""
	if find_existing("Purchase Receipt", connection, receipt_id):
		return True
	return bool(
		frappe.db.exists(
			"Purchase Receipt",
			{
				"koomyo_connection": connection.name,
				"koomyo_extra_ids": ("like", f"%{receipt_id}%"),
				"docstatus": ("<", 2),
			},
		)
	)


def restrict_to_place(pr, po, place: str, order: dict):
	"""Keep the rows for the Warehouse the goods landed at. An order with one place for every row (made in
	ERPNext for one Warehouse) takes the delivery's place, so stock lands where Koomyo put it."""
	here = [item for item in pr.items if item.warehouse == place]
	if here:
		for item in list(pr.items):
			if item.warehouse != place:
				pr.remove(item)
		return
	warehouses = {item.warehouse for item in pr.items}
	if len(warehouses) == 1:
		for item in pr.items:
			item.warehouse = place
		return
	raise NeedsMapping(
		_(
			"Koomyo delivered purchase order {0} to Warehouse {1}, but no row of Purchase Order {2} is for it. Check the Branch Map and the order's rows, then press Retry."
		).format(order.get("number") or order["id"], place, po.name)
	)


def link_unstamped_receipt(po, order: dict, receipt: dict, connection) -> bool:
	"""A Purchase Receipt made in ERPNext and sent to Koomyo is this delivery: link it, book nothing."""
	filters = {"purchase_order": po.name, "docstatus": 1}
	place = receipt_warehouse(connection, receipt)
	if place:
		filters["warehouse"] = place  # an order received once for each place: the receipt for this one
	candidates = frappe.get_all("Purchase Receipt Item", filters=filters, pluck="parent", distinct=True)
	for name in candidates:
		if frappe.db.get_value("Purchase Receipt", name, "koomyo_id"):
			continue
		frappe.db.set_value(
			"Purchase Receipt",
			name,
			{"koomyo_connection": connection.name, "koomyo_id": receipt["id"]},
			update_modified=False,
		)
		return True
	return False


def apply_delivery_costs(pr, po, order, receipt, connection, delivered_value) -> list[dict]:
	"""Give the receipt its share of the tax and charge rows; return the Landed Cost Voucher lines."""
	if not po.taxes:
		return []
	cost_center = po.taxes[0].cost_center or order_cost_center(connection, order)
	rows = tax_rows(connection, order, cost_center or "")
	ordered_value = sum((e["net"] for e in order_lines(order)), ZERO)
	ordered_qty = sum((e["qty"] for e in order_lines(order)), ZERO)
	qty = sum((exact(l.get("quantity"), "quantity") for l in receipt.get("lines") or []), ZERO)
	delivered_before = delivered_so_far(po, connection)
	final = order.get("status") == "received" and receipts_after(order, receipt) == 0
	pr.set("taxes", [])
	vouchers = []
	for row in rows:
		basis = row.pop("basis")
		total = Decimal(str(row["tax_amount"]))
		if basis == "quantity":
			applied = applied_before(po, row["description"], connection)
			amount = share(total, delivered_before["qty"] + qty, ordered_qty, applied, final)
			if amount:
				vouchers.append(
					{
						"account": row["account_head"],
						"description": row["description"],
						"amount": amount,
						"add": row["add_deduct_tax"] == "Add",
					}
				)
			continue
		applied = applied_before(po, row["description"], connection)
		amount = share(total, delivered_before["value"] + delivered_value, ordered_value, applied, final)
		if not amount:
			continue
		row["tax_amount"] = to_field(amount)
		pr.append("taxes", row)
	return vouchers


def delivered_so_far(po, connection) -> dict:
	value = ZERO
	qty = ZERO
	for item in frappe.get_all(
		"Purchase Receipt Item",
		filters={"purchase_order": po.name, "docstatus": 1},
		fields=["stock_qty", "rate"],
	):
		qty += Decimal(str(item.stock_qty))
		value += Decimal(str(item.stock_qty)) * Decimal(str(item.rate))
	return {"value": value, "qty": qty}


def applied_before(po, description: str, connection) -> Decimal:
	"""What earlier receipts of this order already took of one tax or charge row."""
	total = ZERO
	for parent in received_against(po):
		for row in frappe.get_all(
			"Purchase Taxes and Charges",
			filters={"parent": parent, "parenttype": "Purchase Receipt", "description": description},
			fields=["tax_amount"],
		):
			total += Decimal(str(row.tax_amount))
		for voucher in frappe.get_all(
			"Landed Cost Taxes and Charges",
			filters={
				"description": description,
				"parenttype": "Landed Cost Voucher",
				"parent": ("in", landed_vouchers_for(parent)),
			},
			fields=["amount"],
		):
			total += Decimal(str(voucher.amount))
	return total


def landed_vouchers_for(receipt_name: str) -> list[str]:
	names = frappe.get_all(
		"Landed Cost Purchase Receipt",
		filters={"receipt_document": receipt_name, "docstatus": 1},
		pluck="parent",
	)
	return names or ["-"]


def receipts_after(order: dict, receipt: dict) -> int:
	ids = [r.get("id") for r in order.get("receipts") or []]
	if receipt["id"] not in ids:
		return 0
	return len(ids) - 1 - ids.index(receipt["id"])


def make_landed_cost_voucher(pr, voucher: dict, connection, receipt_id: str, order: dict):
	"""Charges spread by quantity: a Landed Cost Voucher on the receipt, distributed by Qty."""
	existing = find_existing("Landed Cost Voucher", connection, f"{receipt_id}:{voucher['description']}")
	if existing:
		return existing
	lcv = frappe.new_doc("Landed Cost Voucher")
	lcv.update(
		{
			"company": pr.company,
			"posting_date": pr.posting_date,
			"distribute_charges_based_on": "Qty",
		}
	)
	lcv.append(
		"purchase_receipts",
		{
			"receipt_document_type": "Purchase Receipt",
			"receipt_document": pr.name,
			"supplier": pr.supplier,
			"posting_date": pr.posting_date,
			"grand_total": pr.grand_total,
		},
	)
	lcv.get_items_from_purchase_receipts()
	lcv.append(
		"taxes",
		{
			"expense_account": voucher["account"],
			"description": voucher["description"],
			"amount": to_field(voucher["amount"] if voucher["add"] else -voucher["amount"]),
		},
	)
	stamp(lcv, connection, f"{receipt_id}:{voucher['description']}", pr.koomyo_branch)
	lcv.insert(ignore_permissions=True)
	lcv.submit()
	return lcv.name


# ------------------------------------------------------------------ the safety net


def sync_purchase_orders(connection_name: str, client: KoomyoClient | None = None) -> dict:
	"""Read GET /purchase-orders?updated_since= and queue each changed order through the same path.

	The orders come back in the order they changed, so the position saved on the connection is the
	newest `updated_at` read. Each order becomes an Event Log row (type koomyo.purchase_sync), so it is
	visible, can be retried, and is booked by `apply_order` exactly like a webhook.
	"""
	from koomyo_erpnext.sync.events import enqueue_processing, store_event

	connection = frappe.get_doc("Koomyo Connection", connection_name)
	summary = {"read": 0, "queued": 0}
	if not (connection.enabled and connection.sync_purchasing and connection.tenant_id):
		return summary
	if connection.status == "Needs Re-sync":
		return summary
	since = connection.purchase_sync_cursor or f"{connection.start_from_date}T00:00:00Z"
	client = client or KoomyoClient.for_connection(connection)
	newest = connection.purchase_sync_cursor
	for order in client.iter_list("/purchase-orders", {"updated_since": since}):
		summary["read"] += 1
		if order.get("object") != "purchase_order" or not order.get("id") or not order.get("updated_at"):
			continue
		if order.get("status") == "draft" and not find_existing("Purchase Order", connection, order["id"]):
			continue  # a plan: nothing to book, and nothing is created for a draft
		event = {
			"id": f"po-sync:{order['id']}:{order['updated_at']}",
			"type": SYNC_TYPE,
			"seq": 0,
			"tenant_id": connection.tenant_id,
			"created": order["updated_at"],
			"business_date": order.get("business_date"),
			"branch_id": order.get("branch_id"),
			"entity": {"type": "purchase_order", "id": order["id"]},
			"data": order,
		}
		_name, created = store_event(connection, event, "Feed")
		summary["queued"] += 1 if created else 0
		if not newest or get_datetime(order["updated_at"]) > get_datetime(newest):
			newest = order["updated_at"]
	if newest and newest != connection.purchase_sync_cursor:
		connection.db_set("purchase_sync_cursor", newest, update_modified=False)
	frappe.db.commit()
	if summary["queued"]:
		enqueue_processing(connection_name)
	return summary
