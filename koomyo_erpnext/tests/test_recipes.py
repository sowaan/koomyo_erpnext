"""Section 5: menu options and the stock one line takes (costing and bills of materials only).

Numbers are the docs' own example (guides/menu-options): a Large Chicken Tikka Supreme with a Thin
Crust, whose Large row gives back 0.45 kg of Pizza Dough and takes one dough ball.
"""

from decimal import Decimal as D

from koomyo_erpnext.compat import KoomyoTestCase as FrappeTestCase
from koomyo_erpnext.exceptions import Fatal
from koomyo_erpnext.sync import catalog
from koomyo_erpnext.sync.recipes import (
	chosen_options,
	line_stock_use,
	option_warnings,
	options_by_id,
	use_cost,
)
from koomyo_erpnext.tests.fake_koomyo import FakeKoomyo, error_response
from koomyo_erpnext.tests.helpers import KoomyoIntegrationTestCase

DOUGH, BALL, CHEESE, SAUCE = "ing-dough", "ing-ball", "ing-cheese", "ing-sauce"
LARGE, MEDIUM = "size-L", "size-M"

PIZZA_RECIPE = [
	{
		"size_id": LARGE,
		"ingredient_id": DOUGH,
		"ingredient": "Pizza Dough",
		"unit": "kg",
		"quantity": "0.4500",
	},
	{
		"size_id": MEDIUM,
		"ingredient_id": DOUGH,
		"ingredient": "Pizza Dough",
		"unit": "kg",
		"quantity": "0.3000",
	},
	{"size_id": None, "ingredient_id": SAUCE, "ingredient": "Sauce", "unit": "kg", "quantity": "0.1000"},
]


def swap(size, replaced_qty, quantity="1.0000"):
	return {
		"size_name": size,
		"ingredient_id": BALL,
		"ingredient": "Pizza dough ball",
		"unit": "pcs",
		"quantity": quantity,
		"replaces": {
			"ingredient_id": DOUGH,
			"ingredient": "Pizza Dough",
			"quantity": replaced_qty,
			"unit": "kg",
		},
	}


THIN_CRUST = {
	"id": "opt-thin",
	"name": "Thin Crust",
	"price": "0.00",
	"size_prices": {"L": "-250.00", "M": "-150.00"},
	"available_sizes": ["M", "L"],
	"recipe": [swap("L", "0.4500"), swap("M", "0.3000")],
}
EXTRA_CHEESE = {
	"id": "opt-cheese",
	"name": "Extra Cheese",
	"price": "99.00",
	"size_prices": {},
	"available_sizes": [],
	"recipe": [
		{
			"size_name": None,
			"ingredient_id": CHEESE,
			"ingredient": "Cheese",
			"unit": "kg",
			"quantity": "0.0500",
			"replaces": None,
		}
	],
}


class TestLineStockUse(FrappeTestCase):
	def test_docs_example_thin_crust_on_a_large_gives_the_dough_back_and_takes_a_ball(self):
		use = line_stock_use(PIZZA_RECIPE, LARGE, "L", [THIN_CRUST], 1)
		self.assertEqual(use[DOUGH], D("0"))
		self.assertEqual(use[BALL], D("1"))
		self.assertEqual(use[SAUCE], D("0.1000"))

	def test_medium_uses_its_own_row(self):
		use = line_stock_use(PIZZA_RECIPE, MEDIUM, "m", [THIN_CRUST], 1)
		self.assertEqual((use[DOUGH], use[BALL]), (D("0"), D("1")))

	def test_a_plain_row_and_the_line_quantity(self):
		use = line_stock_use(PIZZA_RECIPE, LARGE, "L", [EXTRA_CHEESE, THIN_CRUST], 3)
		self.assertEqual(use[CHEESE], D("0.1500"))
		self.assertEqual(use[BALL], D("3"))
		self.assertEqual(use[DOUGH], D("0"))
		self.assertEqual(use[SAUCE], D("0.3000"))

	def test_without_options_it_is_the_items_own_recipe_for_that_size(self):
		self.assertEqual(
			line_stock_use(PIZZA_RECIPE, LARGE, "L", [], 2), {DOUGH: D("0.9000"), SAUCE: D("0.2000")}
		)

	def test_size_names_compare_ignoring_capitals_and_spaces(self):
		option = {
			"recipe": [
				{"size_name": " Extra  Large ", "ingredient_id": CHEESE, "quantity": "1", "replaces": None}
			]
		}
		self.assertEqual(line_stock_use([], None, "extra large", [option]), {CHEESE: D("1")})
		self.assertEqual(line_stock_use([], None, "Large", [option]), {})

	def test_a_swap_with_nothing_to_give_back_goes_negative_as_the_docs_do(self):
		use = line_stock_use([], None, "L", [THIN_CRUST], 1)
		self.assertEqual(use[DOUGH], D("-0.4500"))

	def test_a_row_for_every_size_stacks_on_the_size_row(self):
		option = {
			"name": "Double",
			"recipe": [
				{"size_name": None, "ingredient_id": CHEESE, "quantity": "0.1", "replaces": None},
				{"size_name": "L", "ingredient_id": CHEESE, "quantity": "0.2", "replaces": None},
			],
		}
		self.assertEqual(line_stock_use([], None, "L", [option])[CHEESE], D("0.3"))
		self.assertEqual(len(option_warnings({"name": "Extras", "options": [option]})), 1)
		self.assertEqual(option_warnings({"name": "Crust", "options": [THIN_CRUST]}), [])

	def test_no_float_artefacts(self):
		option = {
			"recipe": [{"size_name": None, "ingredient_id": CHEESE, "quantity": "0.1", "replaces": None}]
		}
		self.assertEqual(line_stock_use([], None, None, [option], 3)[CHEESE], D("0.3"))

	def test_bad_quantity_is_a_contract_break_not_a_guess(self):
		with self.assertRaises(Fatal):
			line_stock_use([{"size_id": None, "ingredient_id": DOUGH, "quantity": "lots"}], None, None, [])


class TestChoicesAndCost(FrappeTestCase):
	def test_an_order_lines_modifiers_resolve_by_id_and_unknown_ones_are_reported(self):
		known = options_by_id([{"name": "Crust", "options": [THIN_CRUST, EXTRA_CHEESE]}])
		line = {
			"modifiers": [
				{"modifier_id": "opt-thin", "name": "Thin Crust", "price": "-250.00"},
				{"modifier_id": None, "name": "No onion", "price": "0.00"},
				{"modifier_id": "gone", "name": "Old option", "price": "10.00"},
			]
		}
		found, unknown = chosen_options(line, known)
		self.assertEqual([o["name"] for o in found], ["Thin Crust"])
		self.assertEqual(unknown, ["No onion", "Old option"])

	def test_cost_is_exact_and_names_what_has_no_cost(self):
		use = line_stock_use(PIZZA_RECIPE, LARGE, "L", [EXTRA_CHEESE, THIN_CRUST], 1)
		cost, missing = use_cost(use, {DOUGH: D("200.0000"), BALL: D("35.0000"), CHEESE: D("1450.0000")})
		self.assertEqual(cost, D("0") * D("200") + D("35") + D("72.5"))
		self.assertEqual(missing, [SAUCE])


class TestReadingOptions(KoomyoIntegrationTestCase):
	def group(self):
		return {
			"id": "grp-crust",
			"object": "option_group",
			"name": "Crust",
			"display_name": None,
			"active": True,
			"options": [THIN_CRUST, {**EXTRA_CHEESE, "recipe": []}],
		}

	def test_the_client_reads_groups_by_cursor_and_one_group(self):
		from koomyo_erpnext.client import KoomyoClient

		self.fake_a.lists["/option-groups"] = [self.group()]
		with self.koomyo():
			client = KoomyoClient.for_connection(self.conn_a)
			groups = client.list_option_groups(item_id="menu-1", active=True)
		self.assertEqual([g["id"] for g in groups], ["grp-crust"])
		(call,) = [c for c in self.fake_a.calls if c["path"] == "/option-groups"]
		self.assertEqual((call["params"]["item_id"], call["params"]["active"]), ("menu-1", "true"))
		with self.koomyo():
			self.assertEqual(client.get_option_group("grp-crust")["name"], "Crust")

	def test_the_menu_load_reports_options_and_never_writes_them(self):
		self.fake_a.lists["/option-groups"] = [self.group()]
		summary = {"problems": []}
		from koomyo_erpnext.client import KoomyoClient

		with self.koomyo():
			catalog.read_options(KoomyoClient.for_connection(self.conn_a), summary)
		self.assertEqual(summary["options"], {"groups": 1, "options": 2, "no_recipe": 1, "warnings": []})
		self.assertEqual(self.fake_a.writes, [])

	def test_an_api_without_options_is_left_alone_and_other_errors_are_reported(self):
		from koomyo_erpnext.client import KoomyoClient

		summary = {"problems": []}
		with self.koomyo():
			catalog.read_options(
				KoomyoClient.for_connection(self.conn_a), summary
			)  # the fake has no such list: 404
		self.assertEqual((summary.get("options"), summary["problems"]), (None, []))
		self.fake_a.scripted["/option-groups"] = [error_response(403, "forbidden", "scope catalog.read")]
		with self.koomyo():
			catalog.read_options(KoomyoClient.for_connection(self.conn_a), summary)
		self.assertEqual(len(summary["problems"]), 1)
		self.assertIn("Options could not be read", summary["problems"][0])
