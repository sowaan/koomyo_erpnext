"""A transfer that fulfils a Koomyo purchase request line (request_line_id), made from the Material Request.

Koomyo enforces the limits; the app asks first (a pre-check), sends the line id, shows Koomyo's refusals as plain
sentences, and records the transfer on the Material Request.
"""

import copy
from decimal import Decimal as D

import frappe

from koomyo_erpnext import api
from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry
from koomyo_erpnext.tests.test_purchase_requests import (
	LINE_1,
	REQUEST_ID,
	make_request,
	request_event,
)
from koomyo_erpnext.tests.test_stock import CENTRAL, MOZZARELLA, STORES
from koomyo_erpnext.tests.test_transfers_out import TransfersOutCase


class TransferFromRequestCase(TransfersOutCase):
	def setUp(self):
		super().setUp()
		self.conn_a.reload()
		self.conn_a.update({"sync_purchasing": 1, "sync_purchase_requests": 1})
		self.conn_a.save()
		self.card = make_request()  # 8 kg of mozzarella asked, 6 approved, for the Gulberg branch (Stores)
		self.fake_a.lists["/purchase-requests"] = [copy.deepcopy(self.card)]
		self.seq += 1
		log = self.book(request_event(self.card, "purchase_request.approved", self.seq))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		name = frappe.db.get_value(
			"Material Request", {"koomyo_connection": self.conn_a.name, "koomyo_id": REQUEST_ID}
		)
		self.mr = frappe.get_doc("Material Request", name)
		self.fake_a.writes.clear()

	def tearDown(self):
		frappe.db.delete("Comment", {"reference_doctype": "Material Request", "reference_name": self.mr.name})
		super().tearDown()

	def request_line(self):
		return self.fake_a.lists["/purchase-requests"][0]["lines"][0]

	def make_entry_from_request(self, qty=None, source=CENTRAL, submit=True):
		name = api.make_transfer_from_request(self.mr.name, source)
		entry = frappe.get_doc("Stock Entry", name)
		if qty is not None:
			entry.items[0].qty = qty
			entry.save()
		if submit:
			entry.submit()
		frappe.db.commit()
		self.run_queue()
		return entry


class TestFromRequest(TransferFromRequestCase):
	def test_the_button_builds_one_row_per_request_row_to_the_branch_warehouse(self):
		name = api.make_transfer_from_request(self.mr.name, CENTRAL)
		entry = frappe.get_doc("Stock Entry", name)
		self.assertEqual((entry.docstatus, entry.purpose), (0, "Material Transfer"))
		(row,) = entry.items
		self.assertEqual((row.s_warehouse, row.t_warehouse, row.qty), (CENTRAL, STORES, 6))
		self.assertEqual(
			(row.material_request, row.material_request_item), (self.mr.name, self.mr.items[0].name)
		)

	def test_the_transfer_names_the_request_line_and_is_recorded_on_the_material_request(self):
		entry = self.make_entry_from_request(4)
		(log,) = self.outbound_logs("erpnext.stock_transfer")
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		(transfer,) = self.transfers()
		(line,) = transfer["lines"]
		self.assertEqual(
			(line["ingredient_id"], line["quantity_sent"], line["request_line_id"]),
			(MOZZARELLA, "4.000", LINE_1),
		)
		self.assertEqual(self.fake_a.transfer_calls[0][1]["lines"][0]["request_line_id"], LINE_1)
		notes = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Material Request", "reference_name": self.mr.name},
			pluck="content",
		)
		self.assertTrue([n for n in notes if entry.name in n and transfer["number"] in n])
		self.assertEqual(D(self.request_line()["ordered_quantity"]), D(4))

	def test_a_repeated_push_makes_no_second_transfer(self):
		self.make_entry_from_request(4)
		(log,) = self.outbound_logs("erpnext.stock_transfer")
		retry([log.name])
		self.run_queue()
		self.assertEqual(len(self.transfers()), 1)
		self.assertEqual(D(self.request_line()["ordered_quantity"]), D(4))

	def test_koomyo_refusing_an_over_quantity_is_said_plainly_and_nothing_is_made(self):
		# the pre-check reads 6 still needed; Koomyo then counts a purchase order made meanwhile
		entry = self.make_entry_from_request(5, submit=False)
		self.request_line()["ordered_quantity"] = "3"
		self.fake_a.lists["/purchase-requests"][0]["lines"][0]["ordered_quantity"] = (
			"1"  # the read says 5 left
		)
		entry.submit()
		frappe.db.commit()
		self.request_line()["ordered_quantity"] = "3"  # ... but only 3 are left when pushed
		self.run_queue()
		(log,) = self.outbound_logs("erpnext.stock_transfer")
		self.assertEqual(log.status, "Failed")
		self.assertIn("needs less than the quantity sent", log.message)
		self.assertIn("Koomyo said:", log.message)
		self.assertEqual(self.transfers(), [])
		self.assertFalse(frappe.db.get_value("Stock Entry", entry.name, "koomyo_sent"))

	def test_a_line_that_is_not_open_is_refused_by_the_pre_check_before_submit(self):
		entry = self.make_entry_from_request(submit=False)
		self.request_line()["status"] = "rejected"
		with self.koomyo():
			with self.assertRaises(frappe.ValidationError) as caught:
				entry.submit()
		self.assertIn("only approved, ordered or partly received", str(caught.exception))

	def test_more_than_still_needed_is_refused_by_the_pre_check(self):
		entry = self.make_entry_from_request(submit=False)
		entry = frappe.get_doc("Stock Entry", entry.name)
		entry.items[0].qty = 7
		entry.save()
		with self.koomyo():
			with self.assertRaises(frappe.ValidationError) as caught:
				entry.submit()
		self.assertIn("more than Koomyo still needs", str(caught.exception))

	def test_another_destination_is_refused_by_the_pre_check(self):
		entry = self.make_entry_from_request(submit=False)
		entry = frappe.get_doc("Stock Entry", entry.name)
		entry.items[0].t_warehouse = "Work In Progress - _KTA"
		entry.to_warehouse = "Work In Progress - _KTA"
		entry.save()
		with self.koomyo():
			with self.assertRaises(frappe.ValidationError) as caught:
				entry.submit()
		self.assertIn("request's branch", str(caught.exception))

	def test_when_koomyo_cannot_be_read_the_pre_check_only_warns(self):
		entry = self.make_entry_from_request(submit=False)
		self.fake_a.down = True
		with self.koomyo():
			entry.submit()  # not blocked: Koomyo's answer on the push is the final word
		self.fake_a.down = False
		self.assertEqual(frappe.db.get_value("Stock Entry", entry.name, "docstatus"), 1)

	def test_purchase_requests_switched_off_in_koomyo_say_so(self):
		entry = self.make_entry_from_request(submit=False)
		self.fake_a.purchase_requests_off = True
		# the pre-check cannot read the request either, so it only warns; the push gets Koomyo's refusal
		with self.koomyo():
			entry.submit()
		frappe.db.commit()
		self.run_queue()
		(log,) = self.outbound_logs("erpnext.stock_transfer")
		self.assertEqual(log.status, "Failed")
		self.assertIn("purchase requests are switched off", log.message)

	def test_a_transfer_without_a_request_line_sends_none(self):
		self.make_entry(CENTRAL, STORES, 2)
		self.run_queue()
		self.assertNotIn("request_line_id", self.fake_a.transfer_calls[-1][1]["lines"][0])

	def test_cancelling_the_transfer_gives_the_quantity_back(self):
		entry = self.make_entry_from_request(4)
		transfer = self.transfers()[0]
		transfer["receipts"].clear()
		transfer["lines"][0].update({"quantity_received": "0.000", "quantity_open": "4.000"})
		transfer["status"] = "in_transit"
		frappe.get_doc("Stock Entry", entry.name).cancel()
		frappe.db.commit()
		self.run_queue()
		self.assertEqual(D(self.request_line()["ordered_quantity"]), D(0))


class TestPurchaseOrderAfterTransfer(TransferFromRequestCase):
	"""ERPNext's Create > Purchase Order must not order what a transfer already covers."""

	def po_rows(self):
		from koomyo_erpnext.overrides import make_purchase_order

		return make_purchase_order(self.mr.name).items

	def test_a_row_the_transfer_covers_in_full_is_left_off_and_a_part_is_taken_off(self):
		self.make_entry_from_request(4)
		(row,) = self.po_rows()
		self.assertEqual(D(str(row.qty)), D(2))  # 6 approved, 4 by transfer

	def test_a_row_that_is_all_transferred_is_dropped(self):
		from koomyo_erpnext.tests.test_purchase_requests import CHEESE, FLOUR, line

		# two lines: mozzarella 6 (transferred in full) and flour 3
		self.fake_a.add_ingredient(FLOUR["id"], "Flour", "kg", "90.0000")
		card = make_request(
			"approved",
			"2026-10-26T11:00:00Z",
			lines=[
				line(LINE_1, CHEESE, "6.000", "6.000"),
				line("5d7a1c62-0b1e-4f0e-9d33-2f6f5a4b8e02", FLOUR, "3.000", "3.000"),
			],
		)
		self.fake_a.lists["/purchase-requests"] = [copy.deepcopy(card)]
		self.seq += 1
		self.book(request_event(card, "purchase_request.updated", self.seq))
		name = frappe.db.get_value(
			"Material Request",
			{"koomyo_connection": self.conn_a.name, "koomyo_id": REQUEST_ID, "docstatus": 1},
		)
		self.mr = frappe.get_doc("Material Request", name)
		self.assertEqual(len(self.mr.items), 2)
		entry_name = api.make_transfer_from_request(self.mr.name, CENTRAL)
		entry = frappe.get_doc("Stock Entry", entry_name)
		entry.items = [r for r in entry.items if r.item_code == self.mr.items[0].item_code]
		entry.save()
		entry.submit()
		frappe.db.commit()
		rows = self.po_rows()
		self.assertEqual([r.item_code for r in rows], [self.mr.items[1].item_code])
		self.assertEqual([r.idx for r in rows], [1])

	def test_when_everything_is_covered_it_says_so(self):
		self.make_entry_from_request()  # all 6
		with self.assertRaises(frappe.ValidationError) as caught:
			self.po_rows()
		self.assertIn("already covered by transfers", str(caught.exception))

	def test_a_material_request_not_from_koomyo_is_untouched(self):
		from koomyo_erpnext.overrides import take_off_transfers

		self.assertTrue(callable(take_off_transfers))
		frappe.db.set_value("Material Request", self.mr.name, "koomyo_id", None)
		try:
			from koomyo_erpnext.overrides import make_purchase_order

			self.assertEqual(D(str(make_purchase_order(self.mr.name).items[0].qty)), D(6))
		finally:
			frappe.db.set_value("Material Request", self.mr.name, "koomyo_id", REQUEST_ID)

	def test_the_button_route_is_allowed(self):
		"""ERPNext's button goes through frappe.model.mapper.make_mapped_doc, which refuses a method that is not whitelisted."""
		from frappe.model.mapper import make_mapped_doc

		self.make_entry_from_request(4)
		doc = make_mapped_doc(
			"erpnext.stock.doctype.material_request.material_request.make_purchase_order", self.mr.name
		)
		self.assertEqual([D(str(r.qty)) for r in doc.items], [D(2)])
