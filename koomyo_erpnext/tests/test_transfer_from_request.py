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
