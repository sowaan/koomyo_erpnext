"""Item 1: a sent Purchase Order that ERPNext changes is edited in Koomyo (PATCH /purchase-orders/{id}).

ERPNext lets a submitted order change through Update Items (quantity and rate of rows, new rows, removed
rows). Koomyo takes the complete lines (every existing line id kept), the complete charges, the tax and the
`updated_at` last read.
"""

import json

import frappe
from frappe.utils import flt

from koomyo_erpnext.tests.test_purchase_requests import MOZZARELLA
from koomyo_erpnext.tests.test_purchasing import PurchasingTestCase


def update_items(po, rows):
	"""ERPNext's Update Items on a submitted order: [{"docname", "item_code", "qty", "rate"}]."""
	try:
		from erpnext.controllers.accounts_controller import update_child_qty_rate as update
	except ImportError:  # the name in other versions
		from erpnext.controllers.accounts_controller import update_child_qty_and_rate as update

	update("Purchase Order", json.dumps(rows), po.name)
	frappe.db.commit()


class EditCase(PurchasingTestCase):
	def patches(self):
		return [w["body"] for w in self.fake_a.writes if w["method"] == "PATCH" and w["path"].count("/") == 2]

	def edit_logs(self):
		return [l for l in self.outbound() if l.event_type == "erpnext.purchase_order_edit"]


class TestEdit(EditCase):
	def setUp(self):
		super().setUp()
		self.po = self.make_po(qty=10)
		self.run_queue()
		self.order = self.fake_a.purchase_orders["po-1"]
		self.fake_a.writes.clear()

	def row(self):
		return frappe.get_doc("Purchase Order", self.po.name).items[0]

	def test_a_changed_quantity_is_sent_with_the_line_id_the_complete_list_and_updated_at(self):
		read_at = self.order["updated_at"]
		update_items(
			self.po,
			[{"docname": self.row().name, "item_code": self.row().item_code, "qty": 12, "rate": 1450}],
		)
		self.run_queue()
		(log,) = self.edit_logs()
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		(body,) = self.patches()
		self.assertEqual(body["updated_at"], read_at)
		(line,) = body["lines"]
		self.assertEqual(
			(line["id"], line["ingredient_id"], line["quantity_ordered"]), ("po-1-line-0", MOZZARELLA, "12")
		)
		self.assertEqual(body["charges"], [], "the complete list of charges: none")
		self.assertEqual(self.order["lines"][0]["quantity_ordered"], "12")
		self.assertEqual(len(self.order["lines"]), 1)

	def test_a_new_row_has_no_id_and_a_removed_row_is_left_out(self):
		# add a second ingredient row, keep the first
		self.fake_a.add_ingredient("ing-extra", "Flour", "kg", "90.0000")
		from koomyo_erpnext.sync.stock import ingredient_item

		with self.koomyo():
			flour = ingredient_item(self.conn_a, {"ingredient_id": "ing-extra"})
		update_items(
			self.po,
			[
				{"docname": self.row().name, "item_code": self.row().item_code, "qty": 10, "rate": 1450},
				{"docname": "", "item_code": flour, "qty": 5, "rate": 90},
			],
		)
		self.run_queue()
		body = self.patches()[-1]
		lines = {l["ingredient_id"]: l for l in body["lines"]}
		self.assertEqual(lines[MOZZARELLA]["id"], "po-1-line-0")
		self.assertNotIn("id", lines["ing-extra"], "a line without an id is new")
		self.assertEqual({l["ingredient_id"] for l in self.order["lines"]}, {MOZZARELLA, "ing-extra"})

	def test_a_stale_version_is_read_again_once_and_repeated(self):
		self.fake_a.stale_once = True
		update_items(
			self.po,
			[{"docname": self.row().name, "item_code": self.row().item_code, "qty": 11, "rate": 1450}],
		)
		self.run_queue()
		(log,) = self.edit_logs()
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		bodies = self.patches()
		self.assertEqual(len(bodies), 2, "the refused call, then the repeat on a fresh read")
		self.assertNotEqual(bodies[0]["updated_at"], bodies[1]["updated_at"])
		self.assertEqual(bodies[1]["updated_at"], "2026-10-30T09:59:00Z", "the fresh updated_at was used")
		self.assertEqual(self.order["lines"][0]["quantity_ordered"], "11")

	def test_a_partly_received_order_gets_only_notes_and_expected_date(self):
		self.order["status"] = "partial"
		self.order["lines"][0]["quantity_received"] = "4"
		update_items(
			self.po,
			[{"docname": self.row().name, "item_code": self.row().item_code, "qty": 12, "rate": 1450}],
		)
		frappe.db.set_value("Purchase Order", self.po.name, "schedule_date", "2026-11-20")
		self.run_queue()
		(log,) = self.edit_logs()
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertIn("frozen", log.message)
		self.assertIn("quantity 12", log.message)
		(body,) = self.patches()
		self.assertLessEqual(set(body) - {"updated_at"}, {"notes", "expected_date"})
		self.assertEqual(body["expected_date"], "2026-11-20")
		self.assertEqual(self.order["lines"][0]["quantity_ordered"], "10", "the line was not touched")

	def test_a_received_or_cancelled_order_is_locked_and_nothing_is_sent(self):
		self.order["status"] = "received"
		update_items(
			self.po,
			[{"docname": self.row().name, "item_code": self.row().item_code, "qty": 12, "rate": 1450}],
		)
		self.run_queue()
		(log,) = self.edit_logs()
		self.assertEqual((log.status, log.error_class), ("Failed", "Fatal"))
		self.assertIn("locked", log.message)
		self.assertEqual(self.patches(), [])

	def test_an_order_koomyo_made_or_never_sent_is_not_edited_there(self):
		frappe.db.set_value("Purchase Order", self.po.name, "koomyo_content", "abc")
		update_items(
			self.po,
			[{"docname": self.row().name, "item_code": self.row().item_code, "qty": 12, "rate": 1450}],
		)
		self.run_queue()
		self.assertEqual(self.edit_logs(), [])

	def test_taxes_and_charges_are_sent_as_the_complete_lists(self):
		po = frappe.get_doc("Purchase Order", self.po.name)
		self.assertTrue(flt(po.grand_total))
		# remove nothing: a Purchase Order without taxes sends tax_rate 0 and charges []
		update_items(
			self.po, [{"docname": self.row().name, "item_code": self.row().item_code, "qty": 9, "rate": 1450}]
		)
		self.run_queue()
		body = self.patches()[-1]
		self.assertEqual((body["tax_rate"], body["charges"]), ("0", []))
