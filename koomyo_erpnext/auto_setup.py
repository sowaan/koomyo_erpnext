"""Set Up From Koomyo: read the restaurant, propose the maps and defaults, apply what the user ticks.

Plan (writes nothing): reads GET /account, /branches, /payment-methods and the units of
GET /ingredients, and proposes for each empty setting of the connection either an existing
record of its Company (matched by name) or a new one, or leaves it for the user to choose.

Apply: creates and fills only what the user chose, in one transaction.
- It fills empty settings only and never overwrites or deletes anything.
- Every record it creates is listed on the connection (Koomyo Setup Log) so Undo can
  remove those nothing uses.
- Only the connection's Company is touched.
- It never switches a sync on. On a connection with nothing switched on yet, Dry Run is turned on.

Docs: docs.koomyo.com (conventions, reference/scopes); OpenAPI /branches, /payment-methods, /ingredients.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime, today

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import KoomyoAPIError

USE, CREATE, SKIP = "Use existing", "Create", "Skip"

# Undo order: what points at other records first, accounts and groups last.
UNDO_ORDER = {
	"Mode of Payment": 0,
	"Customer": 1,
	"Warehouse": 2,
	"Cost Center": 2,
	"Price List": 3,
	"Item Group": 3,
	"Supplier Group": 3,
	"Account": 4,
}

# Unit text from Koomyo -> UOM names to look for in ERPNext, in order.
UOM_ALIASES = {
	"kg": ["Kg", "Kilogram"],
	"g": ["Gram", "Gm"],
	"gm": ["Gram", "Gm"],
	"l": ["Litre", "Liter"],
	"ltr": ["Litre", "Liter"],
	"ml": ["Millilitre", "Ml"],
	"pcs": ["Nos", "Unit", "Piece"],
	"pc": ["Nos", "Unit", "Piece"],
	"unit": ["Unit", "Nos"],
	"each": ["Nos", "Unit"],
}

# Account fields: field -> (names to look for, account type to create with, root type, parent group names, new name)
ACCOUNT_DEFAULTS = {
	"delivery_fee_account": (
		["Delivery Income", "Delivery Fee", "Delivery Charges"],
		"",
		"Income",
		["Direct Income", "Indirect Income"],
		"Delivery Income",
	),
	"channel_markup_account": (
		["Channel Markup Income", "Channel Markup", "Commission Income"],
		"",
		"Income",
		["Direct Income", "Indirect Income"],
		"Channel Markup Income",
	),
	"payment_adjustment_account": (
		["Payment Method Discounts", "Payment Discounts", "Sales Discount", "Discount Allowed"],
		"",
		"Expense",
		["Indirect Expenses", "Direct Expenses"],
		"Payment Method Discounts",
	),
	"cash_over_short_account": (
		["Cash Over and Short", "Cash Over/Short", "Cash Short and Over", "Cash Difference"],
		"",
		"Expense",
		["Indirect Expenses", "Direct Expenses"],
		"Cash Over and Short",
	),
	"purchase_tax_account": (
		["Input Tax", "Purchase Tax", "Input VAT", "Input GST", "Sales Tax Input"],
		"Tax",
		"Liability",
		["Duties and Taxes"],
		"Input Tax",
	),
	"purchase_withholding_account": (
		["Withholding Tax Payable", "Withholding Tax", "Income Tax Withheld"],
		"Tax",
		"Liability",
		["Duties and Taxes"],
		"Withholding Tax Payable",
	),
	"purchase_charge_account": (
		["Expenses Included In Valuation", "Landed Cost Clearing"],
		"Expenses Included In Valuation",
		"Expense",
		["Stock Expenses", "Direct Expenses", "Indirect Expenses"],
		"Expenses Included In Valuation",
	),
	"purchase_charge_expense_account": (
		["Purchase Charges", "Freight and Forwarding Charges", "Freight Charges"],
		"",
		"Expense",
		["Indirect Expenses", "Direct Expenses"],
		"Purchase Charges",
	),
	"transfer_loss_account": (
		["Stock Transfer Loss", "Transfer Loss", "Stock Loss"],
		"",
		"Expense",
		["Indirect Expenses", "Direct Expenses"],
		"Stock Transfer Loss",
	),
}
WASTE_ACCOUNTS = {
	"spoilage": (["Food Waste", "Wastage", "Waste", "Spoilage"], "Food Waste"),
	"tasting": (["Food Waste", "Wastage", "Waste", "Spoilage"], "Food Waste"),
	"other": (["Food Waste", "Wastage", "Waste", "Spoilage"], "Food Waste"),
	"staff_meal": (["Staff Meals", "Staff Meal", "Staff Welfare"], "Staff Meals"),
	"comp": (["Complimentary Food", "Complimentary", "Promotional Expenses"], "Complimentary Food"),
}
LOYALTY = (["Loyalty Reward Discount", "Loyalty Discount", "Reward Discount"], "Loyalty Reward Discount")
DISCOUNT_ACCOUNTS = {
	"promotion": (["Sales Promotions", "Promotional Discount", "Promotions"], "Sales Promotions"),
	"voucher": (["Voucher Discount", "Vouchers"], "Voucher Discount"),
	"manual": (["Staff Discounts", "Manual Discounts", "Staff Discount"], "Staff Discounts"),
	"points_reward": LOYALTY,
	"slice_reward": LOYALTY,
	"gift_item": (["Gift Items Discount", "Gift Items"], "Gift Items Discount"),
}


# ------------------------------------------------------------------ lookups


def company_info(company: str) -> frappe._dict:
	return frappe.get_cached_doc("Company", company)


def find_account(
	company: str, names: list[str], root_type: str | None = None, account_type: str | None = None
):
	"""A leaf Account of the Company whose name matches one of `names` (case-insensitive, with or without the company suffix)."""
	abbr = company_info(company).abbr
	filters = {"company": company, "is_group": 0}
	if root_type:
		filters["root_type"] = root_type
	if account_type:
		filters["account_type"] = account_type
	rows = frappe.get_all("Account", filters=filters, fields=["name", "account_name"])
	by_name = {r.account_name.lower(): r.name for r in rows}
	by_name.update({r.name.lower(): r.name for r in rows})
	for wanted in names:
		for key in (wanted.lower(), f"{wanted} - {abbr}".lower()):
			if key in by_name:
				return by_name[key]
	return None


def parent_group(company: str, root_type: str, preferred: list[str], account_type: str | None = None) -> str:
	"""The group Account to create under: a preferred name, else any group of that type, else the root."""
	abbr = company_info(company).abbr
	for name in preferred:
		full = f"{name} - {abbr}"
		if frappe.db.get_value("Account", full, ["is_group", "company"]) == (1, company):
			return full
	filters = {"company": company, "is_group": 1, "root_type": root_type}
	if account_type:
		filters["account_type"] = account_type
	found = frappe.get_all("Account", filters=filters, pluck="name", order_by="lft desc", limit=1)
	if found:
		return found[0]
	root = frappe.get_all(
		"Account",
		filters={"company": company, "root_type": root_type, "parent_account": ("is", "not set")},
		pluck="name",
		limit=1,
	)
	if not root:
		frappe.throw(_("Company {0} has no {1} accounts.").format(company, _(root_type)))
	return root[0]


def leaf_names(doctype: str, filters: dict | None = None) -> list[str]:
	return frappe.get_all(doctype, filters=filters or {}, pluck="name", limit=500)


# ------------------------------------------------------------------ the plan


def read_koomyo(connection) -> dict:
	"""Everything the plan needs from Koomyo: reads only."""
	client = KoomyoClient.for_connection(connection)
	account = client.get_account()
	scopes = set(account["key"]["scopes"])
	notes = []
	branches = client.list_branches() if "catalog.read" in scopes else []
	methods = client.get("/payment-methods")["data"] if "catalog.read" in scopes else []
	if "catalog.read" not in scopes:
		notes.append(
			_("The key has no catalog.read scope, so branches and payment methods could not be read.")
		)
	units = set()
	if "stock.read" in scopes:
		for ingredient in client.iter_list("/ingredients", {"limit": 500}, max_pages=20):
			if ingredient.get("unit"):
				units.add(ingredient["unit"])
	else:
		notes.append(_("The key has no stock.read scope, so ingredient units could not be read."))
	return {
		"account": account,
		"branches": branches,
		"methods": methods,
		"units": sorted(units),
		"notes": notes,
	}


def build_plan(connection, koomyo: dict) -> list[dict]:
	"""Rows the user can change. Each: id, section, label, doctype, action, value, note."""
	co = connection.company
	info = company_info(co)
	rows: list[dict] = []

	def add(id_, section, label, doctype, action, value="", note=""):
		rows.append(
			{
				"id": id_,
				"section": section,
				"label": label,
				"doctype": doctype,
				"action": action,
				"value": value,
				"note": note,
			}
		)

	def propose_account(id_, section, label, existing, new_name):
		if existing:
			add(id_, section, label, "Account", USE, existing)
		else:
			add(id_, section, label, "Account", CREATE, new_name, _("New account"))

	mapped_branches = {r.koomyo_branch_id for r in connection.branch_map}
	for branch in koomyo["branches"]:
		if branch["id"] in mapped_branches:
			continue
		name = (branch.get("name") or branch["id"])[:100]
		cc = frappe.db.exists("Cost Center", {"company": co, "cost_center_name": name, "is_group": 0})
		wh = frappe.db.exists("Warehouse", {"company": co, "warehouse_name": name, "is_group": 0})
		add(
			f"branch:{branch['id']}:cost_center",
			_("Branches"),
			_("{0}: Cost Center").format(name),
			"Cost Center",
			USE if cc else CREATE,
			cc or name,
		)
		add(
			f"branch:{branch['id']}:warehouse",
			_("Branches"),
			_("{0}: Warehouse").format(name),
			"Warehouse",
			USE if wh else CREATE,
			wh or name,
		)

	mapped_methods = {r.koomyo_method for r in connection.payment_map}
	for method in koomyo["methods"]:
		code = method["code"]
		if code in mapped_methods:
			continue
		label = method.get("label") or code
		mop = next(
			(
				frappe.db.get_value("Mode of Payment", m)
				for m in (label, code)
				if frappe.db.exists("Mode of Payment", m)
			),
			None,
		)
		add(
			f"payment:{code}:mop",
			_("Payment methods"),
			_("{0}: Mode of Payment").format(label),
			"Mode of Payment",
			USE if mop else CREATE,
			mop or label,
		)
		if method.get("settles_as_cash") and info.default_cash_account:
			add(
				f"payment:{code}:account",
				_("Payment methods"),
				_("{0}: Account").format(label),
				"Account",
				USE,
				info.default_cash_account,
				_("Company cash account"),
			)
		else:
			existing = find_account(co, [f"{label} Clearing", label, f"{label} Account"], "Asset")
			propose_account(
				f"payment:{code}:account",
				_("Payment methods"),
				_("{0}: Account").format(label),
				existing,
				f"{label} Clearing",
			)

	if not connection.tax_map:
		existing = find_account(
			co, ["Sales Tax", "Output Tax", "GST", "VAT"], "Liability", "Tax"
		) or find_account(co, [], "Liability", "Tax")
		propose_account("tax:account", _("Taxes"), _("Tax account (any rate)"), existing, "Sales Tax")

	mapped_units = {(r.koomyo_unit or "").strip().lower() for r in connection.uom_map}
	for unit in koomyo["units"]:
		if unit.strip().lower() in mapped_units:
			continue
		uom = next(
			(
				frappe.db.get_value("UOM", n)
				for n in [unit, *UOM_ALIASES.get(unit.strip().lower(), [])]
				if frappe.db.exists("UOM", n)
			),
			None,
		)
		add(
			f"uom:{unit}",
			_("Units"),
			_("Unit {0}").format(unit),
			"UOM",
			USE if uom else SKIP,
			uom or "",
			"" if uom else _("No match: pick a UOM or skip"),
		)

	mapped_reasons = {r.reason for r in connection.waste_map}
	for reason, (names, new_name) in WASTE_ACCOUNTS.items():
		if reason in mapped_reasons:
			continue
		propose_account(
			f"waste:{reason}",
			_("Waste reasons"),
			_("Waste: {0}").format(reason),
			find_account(co, names, "Expense"),
			new_name,
		)

	mapped_sources = {r.source for r in connection.discount_map}
	for source, (names, new_name) in DISCOUNT_ACCOUNTS.items():
		if source in mapped_sources:
			continue
		propose_account(
			f"discount:{source}",
			_("Discounts"),
			_("Discount: {0}").format(source),
			find_account(co, names, "Expense"),
			new_name,
		)

	def account_default(fieldname, label, existing, new_name):
		if connection.get(fieldname):
			return
		propose_account(f"field:{fieldname}", _("Defaults"), label, existing, new_name)

	def label_of(fieldname):
		return _(connection.meta.get_label(fieldname))

	account_default(
		"income_account",
		label_of("income_account"),
		info.default_income_account or find_account(co, ["Sales", "Sales Income"], "Income"),
		"Sales",
	)
	account_default(
		"cogs_account",
		label_of("cogs_account"),
		info.default_expense_account or find_account(co, ["Cost of Goods Sold"], "Expense"),
		"Cost of Goods Sold",
	)
	account_default(
		"stock_adjustment_account",
		label_of("stock_adjustment_account"),
		info.stock_adjustment_account or find_account(co, ["Stock Adjustment"], "Expense"),
		"Stock Adjustment",
	)
	account_default("drawer_account", label_of("drawer_account"), info.default_cash_account, "Cash")
	for fieldname, (names, _type, root, _parents, new_name) in ACCOUNT_DEFAULTS.items():
		account_default(fieldname, label_of(fieldname), find_account(co, names, root), new_name)

	restaurant = koomyo["account"]["name"]
	if not connection.default_cost_center and info.cost_center:
		add(
			"field:default_cost_center",
			_("Defaults"),
			label_of("default_cost_center"),
			"Cost Center",
			USE,
			info.cost_center,
			_("Company default"),
		)
	if not connection.main_warehouse:
		wh = frappe.db.exists("Warehouse", {"company": co, "warehouse_name": "Main Warehouse", "is_group": 0})
		add(
			"field:main_warehouse",
			_("Defaults"),
			label_of("main_warehouse"),
			"Warehouse",
			USE if wh else CREATE,
			wh or "Main Warehouse",
		)
	if not connection.walk_in_customer:
		name = f"{restaurant} Walk-in"[:140]
		add(
			"field:walk_in_customer",
			_("Defaults"),
			label_of("walk_in_customer"),
			"Customer",
			USE if frappe.db.exists("Customer", name) else CREATE,
			name,
		)
	for fieldname, new_name in (
		("menu_item_group", "Koomyo Menu"),
		("ingredient_item_group", "Koomyo Ingredients"),
	):
		if not connection.get(fieldname):
			add(
				f"field:{fieldname}",
				_("Defaults"),
				label_of(fieldname),
				"Item Group",
				USE if frappe.db.exists("Item Group", new_name) else CREATE,
				new_name,
			)
	if not connection.price_list:
		name = f"{restaurant} Selling"[:140]
		add(
			"field:price_list",
			_("Defaults"),
			label_of("price_list"),
			"Price List",
			USE if frappe.db.exists("Price List", name) else CREATE,
			name,
		)
	if not connection.supplier_group:
		add(
			"field:supplier_group",
			_("Defaults"),
			label_of("supplier_group"),
			"Supplier Group",
			USE if frappe.db.exists("Supplier Group", "Koomyo Suppliers") else CREATE,
			"Koomyo Suppliers",
		)
	if not connection.customer_group:
		leaf = frappe.get_all(
			"Customer Group", filters={"is_group": 0}, pluck="name", order_by="name", limit=1
		)
		add(
			"field:customer_group",
			_("Defaults"),
			label_of("customer_group"),
			"Customer Group",
			USE if leaf else SKIP,
			leaf[0] if leaf else "",
			"" if leaf else _("No customer group: pick one"),
		)
	if not connection.territory:
		leaf = info.country if frappe.db.exists("Territory", {"name": info.country, "is_group": 0}) else None
		leaf = (
			leaf
			or (
				frappe.get_all("Territory", filters={"is_group": 0}, pluck="name", order_by="name", limit=1)
				or [None]
			)[0]
		)
		add(
			"field:territory",
			_("Defaults"),
			label_of("territory"),
			"Territory",
			USE if leaf else SKIP,
			leaf or "",
			"" if leaf else _("No territory: pick one"),
		)
	return rows


# ------------------------------------------------------------------ creating records


class Setup:
	"""One apply run: creates records, remembers them, and fills the connection."""

	def __init__(self, connection):
		self.connection = connection
		self.co = connection.company
		self.info = company_info(self.co)
		self.created: list[tuple[str, str, str]] = []
		self.used: list[str] = []

	def remember(self, doctype: str, name: str, note: str = ""):
		self.created.append((doctype, name, note))

	def resolve(self, row: dict) -> str | None:
		"""The record name this row ends up as (made if asked to), or None to leave it blank."""
		action, value = row["action"], (row["value"] or "").strip()
		if action == SKIP or not value:
			return None
		if action == USE:
			self.check_exists(row["doctype"], value)
			return value
		return self.create(row["doctype"], value, row)

	def check_exists(self, doctype: str, name: str):
		if not frappe.db.exists(doctype, name):
			frappe.throw(_("{0} {1} does not exist.").format(_(doctype), frappe.bold(name)))
		company = frappe.get_meta(doctype).has_field("company") and frappe.db.get_value(
			doctype, name, "company"
		)
		if company and company != self.co:
			frappe.throw(
				_("{0} {1} belongs to Company {2}, not {3}.").format(
					_(doctype), frappe.bold(name), company, self.co
				)
			)

	def create(self, doctype: str, name: str, row: dict) -> str:
		maker = getattr(self, "make_" + frappe.scrub(doctype), None)
		if not maker:
			frappe.throw(_("Set Up From Koomyo cannot create a {0}.").format(_(doctype)))
		made = maker(name, row)
		if made is None:
			return None
		return made

	def make_account(self, name, row):
		existing = frappe.db.get_value(
			"Account", {"company": self.co, "account_name": name, "is_group": 0}, "name"
		)
		if existing:
			self.used.append(existing)
			return existing
		kind = row["id"].split(":")
		field = kind[1] if kind[0] == "field" else None
		if kind[0] == "payment":
			root, atype, parents = "Asset", "Bank", ["Bank Accounts"]
		elif kind[0] == "tax":
			root, atype, parents = "Liability", "Tax", ["Duties and Taxes"]
		elif kind[0] in ("waste", "discount"):
			root, atype, parents = "Expense", "", ["Indirect Expenses", "Direct Expenses"]
		elif field in ACCOUNT_DEFAULTS:
			_n, atype, root, parents, _new = ACCOUNT_DEFAULTS[field]
		elif field == "income_account":
			root, atype, parents = "Income", "", ["Direct Income", "Indirect Income"]
		elif field == "drawer_account":
			root, atype, parents = "Asset", "Cash", ["Cash In Hand"]
		else:
			root, atype, parents = "Expense", "", ["Indirect Expenses", "Direct Expenses"]
		parent = parent_group(self.co, root, parents, atype or None)
		account = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": name,
				"company": self.co,
				"parent_account": parent,
				"account_type": atype,
			}
		).insert(ignore_permissions=True)
		self.remember("Account", account.name, _("under {0}").format(parent))
		return account.name

	def make_cost_center(self, name, row):
		existing = frappe.db.get_value(
			"Cost Center", {"company": self.co, "cost_center_name": name, "is_group": 0}
		)
		if existing:
			self.used.append(existing)
			return existing
		root = frappe.get_all(
			"Cost Center",
			filters={"company": self.co, "is_group": 1, "parent_cost_center": ("is", "not set")},
			pluck="name",
			limit=1,
		)
		doc = frappe.get_doc(
			{
				"doctype": "Cost Center",
				"cost_center_name": name,
				"company": self.co,
				"parent_cost_center": root[0],
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
		self.remember("Cost Center", doc.name)
		return doc.name

	def make_warehouse(self, name, row):
		existing = frappe.db.get_value(
			"Warehouse", {"company": self.co, "warehouse_name": name, "is_group": 0}
		)
		if existing:
			self.used.append(existing)
			return existing
		root = f"All Warehouses - {self.info.abbr}"
		doc = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": name,
				"company": self.co,
				"parent_warehouse": root if frappe.db.exists("Warehouse", root) else None,
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
		self.remember("Warehouse", doc.name)
		return doc.name

	def make_mode_of_payment(self, name, row):
		if frappe.db.exists("Mode of Payment", name):
			self.used.append(name)
			return name
		cash = row["id"].startswith("payment:") and any(
			m["code"] == row["id"].split(":")[1] and m.get("settles_as_cash") for m in self._methods
		)
		frappe.get_doc(
			{"doctype": "Mode of Payment", "mode_of_payment": name, "type": "Cash" if cash else "Bank"}
		).insert(ignore_permissions=True)
		self.remember("Mode of Payment", name)
		return name

	def make_customer(self, name, row):
		if frappe.db.exists("Customer", name):
			self.used.append(name)
			return name
		group = self.pick("Customer Group") or frappe.throw(
			_("No Customer Group exists to put the walk-in customer in.")
		)
		territory = self.pick("Territory") or frappe.throw(
			_("No Territory exists to put the walk-in customer in.")
		)
		doc = frappe.get_doc(
			{"doctype": "Customer", "customer_name": name, "customer_group": group, "territory": territory}
		).insert(ignore_permissions=True)
		self.remember("Customer", doc.name)
		return doc.name

	def pick(self, doctype: str):
		return (
			frappe.get_all(doctype, filters={"is_group": 0}, pluck="name", order_by="name", limit=1) or [None]
		)[0]

	def make_item_group(self, name, row):
		if frappe.db.exists("Item Group", name):
			self.used.append(name)
			return name
		doc = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": name,
				"parent_item_group": "All Item Groups",
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
		self.remember("Item Group", doc.name)
		return doc.name

	def make_price_list(self, name, row):
		if frappe.db.exists("Price List", name):
			self.used.append(name)
			return name
		currency = self.info.default_currency
		doc = frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": name,
				"currency": currency,
				"selling": 1,
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
		self.remember("Price List", doc.name)
		return doc.name

	def make_supplier_group(self, name, row):
		if frappe.db.exists("Supplier Group", name):
			self.used.append(name)
			return name
		doc = frappe.get_doc(
			{
				"doctype": "Supplier Group",
				"supplier_group_name": name,
				"parent_supplier_group": "All Supplier Groups",
			}
		).insert(ignore_permissions=True)
		self.remember("Supplier Group", doc.name)
		return doc.name


def apply_plan(connection, koomyo: dict, choices: list[dict]) -> dict:
	"""Create and fill what the user chose. Returns a summary."""
	plan = {row["id"]: row for row in build_plan(connection, koomyo)}
	for choice in choices:
		row = plan.get(choice.get("id"))
		if not row:
			frappe.throw(
				_("{0} is not part of the plan any more. Run Set Up From Koomyo again.").format(
					choice.get("id")
				)
			)
		if choice.get("action") not in (USE, CREATE, SKIP):
			frappe.throw(_("Unknown action {0}.").format(choice.get("action")))
		row["action"] = choice["action"]
		row["value"] = choice.get("value") or ""

	setup = Setup(connection)
	setup._methods = koomyo["methods"]
	resolved = {id_: setup.resolve(row) for id_, row in plan.items()}

	branch_ids = {b["id"]: b for b in koomyo["branches"]}
	for branch_id, branch in branch_ids.items():
		cc, wh = (
			resolved.get(f"branch:{branch_id}:cost_center"),
			resolved.get(f"branch:{branch_id}:warehouse"),
		)
		if cc and wh:
			connection.append(
				"branch_map",
				{
					"location_type": "Branch",
					"koomyo_branch_id": branch_id,
					"koomyo_branch_name": branch.get("name"),
					"cost_center": cc,
					"warehouse": wh,
				},
			)
	for method in koomyo["methods"]:
		mop, account = (
			resolved.get(f"payment:{method['code']}:mop"),
			resolved.get(f"payment:{method['code']}:account"),
		)
		if mop and account:
			add_mode_account(mop, setup.co, account)
			connection.append(
				"payment_map", {"koomyo_method": method["code"], "mode_of_payment": mop, "account": account}
			)
	if resolved.get("tax:account"):
		connection.append("tax_map", {"tax_rate": "", "account_head": resolved["tax:account"]})
	for unit in koomyo["units"]:
		if resolved.get(f"uom:{unit}"):
			connection.append("uom_map", {"koomyo_unit": unit, "uom": resolved[f"uom:{unit}"]})
	for reason in WASTE_ACCOUNTS:
		if resolved.get(f"waste:{reason}"):
			connection.append("waste_map", {"reason": reason, "expense_account": resolved[f"waste:{reason}"]})
	for source in DISCOUNT_ACCOUNTS:
		if resolved.get(f"discount:{source}"):
			connection.append("discount_map", {"source": source, "account": resolved[f"discount:{source}"]})
	for id_, value in resolved.items():
		if id_.startswith("field:") and value and not connection.get(id_.split(":")[1]):
			connection.set(id_.split(":")[1], value)
	if not connection.stock_start_date:
		connection.stock_start_date = today()
	if not connection.start_from_date:
		connection.start_from_date = today()
	if (
		not any(
			connection.get(f)
			for f in (
				"sync_sales",
				"sync_refunds",
				"sync_shifts",
				"sync_stock",
				"sync_purchasing",
				"sync_customers",
			)
		)
		and not connection.enabled
	):
		connection.dry_run = 1  # nothing is switched on yet: start in rehearsal mode
	for doctype, name, note in setup.created:
		connection.append(
			"setup_log",
			{"doctype_name": doctype, "record_name": name, "note": note, "created_on": now_datetime()},
		)
	connection.save()
	return {"created": [f"{d} {n}" for d, n, _note in setup.created], "used": sorted(set(setup.used))}


def add_mode_account(mode_of_payment: str, company: str, account: str):
	mop = frappe.get_doc("Mode of Payment", mode_of_payment)
	if not any(r.company == company for r in mop.accounts):
		mop.append("accounts", {"company": company, "default_account": account})
		mop.save(ignore_permissions=True)


# ------------------------------------------------------------------ undo


def clear_references(connection, doctype: str, name: str) -> list:
	"""Blank the connection's own fields and drop map rows that point at a record, returning what to restore."""
	snapshot = connection.as_dict(convert_dates_to_str=True)
	meta = connection.meta
	for df in meta.get("fields"):
		if df.fieldtype == "Link" and df.options == doctype and connection.get(df.fieldname) == name:
			connection.set(df.fieldname, None)
		elif df.fieldtype == "Table" and df.fieldname != "setup_log":
			child_meta = frappe.get_meta(df.options)
			links = [c.fieldname for c in child_meta.fields if c.fieldtype == "Link" and c.options == doctype]
			if links:
				connection.set(
					df.fieldname,
					[r for r in connection.get(df.fieldname) if all(r.get(f) != name for f in links)],
				)
	return snapshot


def undo_plan(connection) -> dict:
	"""Remove what Set Up created and nothing uses; keep the rest.

	Each record is tried on its own. Several passes, because a record is only free once what
	points at it (a Mode of Payment on its account, say) has been removed first.
	"""
	# A plain list of work: try_remove() reloads the connection when a record is kept, which
	# would bring back log rows already dealt with.
	pending = sorted(
		{(r.doctype_name, r.record_name) for r in connection.setup_log},
		key=lambda pair: (UNDO_ORDER.get(pair[0], 5), pair[1]),
	)
	removed: list[str] = []
	progress = True
	while progress and pending:
		progress = False
		for pair in list(pending):
			doctype, name = pair
			if frappe.db.exists(doctype, name) and not try_remove(connection, doctype, name):
				continue
			pending.remove(pair)
			progress = True
			if not frappe.db.exists(doctype, name):
				removed.append(f"{doctype} {name}")
	kept = [f"{d} {n}" for d, n in pending]
	# Keep log rows only for the records that remain.
	connection.reload()
	keep = set(pending)
	connection.set("setup_log", [r for r in connection.setup_log if (r.doctype_name, r.record_name) in keep])
	connection.save()
	return {"removed": removed, "kept": kept}


def try_remove(connection, doctype: str, name: str) -> bool:
	"""Delete one record if nothing uses it; otherwise leave it and the connection exactly as they were."""
	frappe.db.savepoint("koomyo_undo")
	try:
		clear_references(connection, doctype, name)
		if doctype == "Account":
			# Set Up may have added this account to an existing Mode of Payment for this Company.
			frappe.db.delete(
				"Mode of Payment Account", {"default_account": name, "company": connection.company}
			)
		connection.save()
		frappe.delete_doc(doctype, name, ignore_permissions=True)
		return True
	except (frappe.LinkExistsError, frappe.ValidationError):
		frappe.db.rollback(save_point="koomyo_undo")
		frappe.clear_last_message()
		connection.reload()
		return False


# ------------------------------------------------------------------ checklist


def checklist(connection) -> list[dict]:
	"""What is still missing, in plain words, each with the tab to open."""
	todo = []

	def need(tab, text):
		todo.append({"tab": tab, "text": text})

	if connection.status == "Not Tested" or not connection.tenant_id:
		need("tab_connection", _("Press Test Connection."))
	if connection.missing_scopes:
		need("tab_status", _("The API key is missing: {0}.").format(connection.missing_scopes))
	if not connection.branch_map:
		need("tab_setup", _("Branch Map is empty."))
	if (connection.sync_sales or connection.sync_refunds) and not connection.payment_map:
		need("tab_setup", _("Payment Map is empty."))
	if (connection.sync_sales or connection.sync_refunds) and not connection.tax_map:
		need("tab_setup", _("Tax Map is empty."))
	if connection.sync_sales and not connection.discount_map:
		need("tab_setup", _("Discount Map is empty."))
	if connection.sync_stock and not connection.uom_map:
		need("tab_syncs", _("UOM Map is empty."))
	if connection.sync_stock and not connection.waste_map:
		need("tab_syncs", _("Waste Map is empty."))
	required = {
		"sync_sales": ("walk_in_customer", "menu_item_group", "income_account", "price_list"),
		"sync_shifts": ("cash_over_short_account",),
		"sync_stock": (
			"stock_start_date",
			"ingredient_item_group",
			"cogs_account",
			"stock_adjustment_account",
		),
		"sync_customers": ("customer_group", "territory"),
		"book_koomyo_purchases": ("main_warehouse", "supplier_group"),
	}
	for switch, fields in required.items():
		if connection.get(switch):
			for fieldname in fields:
				if not connection.get(fieldname):
					need(
						"tab_syncs",
						_("{0} is needed for {1}.").format(
							_(connection.meta.get_label(fieldname)), _(connection.meta.get_label(switch))
						),
					)
	if not connection.webhook_secret:
		need(
			"tab_webhook",
			_(
				"Paste the webhook secret from Koomyo (without it webhooks are refused; the 5-minute feed still works)."
			),
		)
	return todo


def safe_read(connection):
	try:
		return read_koomyo(connection), None
	except KoomyoAPIError as e:
		from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_connection.koomyo_connection import (
			connection_error_message,
		)

		return None, connection_error_message(e)
