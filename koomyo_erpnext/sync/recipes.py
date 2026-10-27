"""Menu options and the stock one order line uses: for costing and bills of materials only.

Docs: docs.koomyo.com/guides/menu-options (section "The stock one line takes"), OpenAPI
GET /option-groups, GET /option-groups/{id}, GET /menu-items?include=recipe,options, schemas
OptionGroup, Option, MenuItem.

Koomyo deducts ingredient stock itself (its own stock movements, see sync/order_stock.py). Nothing
here moves stock or posts a document: these are the same arithmetic as Koomyo's own, in exact
Decimals, so that a cost or a bill of materials can be worked out from the menu without
guessing, and checked against what Koomyo deducted. No event is sent when an option changes, so
a copy has to be read again on a schedule (`GET /option-groups`).

For one unit of a line with a size and the options the customer chose:

1. the item's own recipe rows for that size (a row with no `size_id` applies to every size);
2. each chosen option's recipe rows that apply to the line's size (`size_name` null applies to
   every size, a named size to that size only; sizes compare by name, ignoring capitals and extra
   spaces);
3. a plain row (`replaces` null) takes `quantity` of its ingredient; a swap row gives back
   `replaces.quantity` of the replaced ingredient (the item's own recipe already took it, so the
   net use can be negative) and takes `quantity` of its own;
4. multiply by the line's quantity.

A row with no size applies on top of the rows for one size: an ingredient given both for every
size and for Large is taken twice on a Large. Koomyo does not change what an option does, so the
same arithmetic is used; `option_warnings` points such rows out.
"""

from decimal import Decimal

from koomyo_erpnext.sync.common import money


def norm(size) -> str:
	"""Size names compare ignoring capitals and extra spaces."""
	return " ".join(str(size or "").lower().split())


def line_stock_use(
	item_recipe: list[dict],
	size_id: str | None,
	size_name: str | None,
	chosen_options: list[dict],
	quantity: int | Decimal = 1,
) -> dict[str, Decimal]:
	"""Ingredient id -> quantity used by `quantity` units of the line (may be negative for a swap)."""
	use: dict[str, Decimal] = {}

	def add(ingredient_id: str, amount: Decimal):
		use[ingredient_id] = use.get(ingredient_id, Decimal(0)) + amount

	for row in item_recipe:
		if not row.get("size_id") or row["size_id"] == size_id:
			add(row["ingredient_id"], money(row.get("quantity"), "recipe quantity"))
	for option in chosen_options:
		for row in option.get("recipe") or []:
			if row.get("size_name") is not None and norm(row["size_name"]) != norm(size_name):
				continue
			add(row["ingredient_id"], money(row.get("quantity"), "option recipe quantity"))
			replaced = row.get("replaces")
			if replaced:
				add(replaced["ingredient_id"], -money(replaced.get("quantity"), "replaced quantity"))
	factor = Decimal(quantity)
	return {ingredient_id: amount * factor for ingredient_id, amount in use.items()}


def options_by_id(groups: list[dict]) -> dict[str, dict]:
	"""Every option of every group by its id (an order line's `modifier_id`)."""
	return {
		option["id"]: option for group in groups for option in group.get("options") or [] if option.get("id")
	}


def chosen_options(line: dict, known: dict[str, dict]) -> tuple[list[dict], list[str]]:
	"""The options an order line chose, and the names of modifiers that could not be matched.

	A modifier without an id, or one Koomyo no longer has, is reported, never guessed.
	"""
	found, unknown = [], []
	for modifier in line.get("modifiers") or []:
		option = known.get(modifier.get("modifier_id") or "")
		if option:
			found.append(option)
		else:
			unknown.append(modifier.get("name") or str(modifier.get("modifier_id")))
	return found, unknown


def use_cost(use: dict[str, Decimal], unit_costs: dict[str, Decimal]) -> tuple[Decimal, list[str]]:
	"""The cost of a use in exact Decimals, and the ingredient ids that have no cost on file."""
	total, missing = Decimal(0), []
	for ingredient_id, amount in use.items():
		if ingredient_id not in unit_costs:
			missing.append(ingredient_id)
			continue
		total += amount * unit_costs[ingredient_id]
	return total, missing


def option_warnings(group: dict) -> list[str]:
	"""Recipe rows that stack: the same ingredient for every size and for one size on the same option."""
	warnings = []
	for option in group.get("options") or []:
		every, sized = set(), {}
		for row in option.get("recipe") or []:
			if row.get("size_name") is None:
				every.add(row["ingredient_id"])
			else:
				sized.setdefault(norm(row["size_name"]), set()).add(row["ingredient_id"])
		for size, ingredients in sized.items():
			if every & ingredients:
				warnings.append(
					f"{group.get('name')}: {option.get('name')} takes an ingredient for every size and for {size}"
				)
	return warnings
