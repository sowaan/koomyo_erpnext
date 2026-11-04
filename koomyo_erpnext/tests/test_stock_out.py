"""Item 7: physical stock changes made in ERPNext and marked for Koomyo (adjustment, waste, supply issue, count).

Opt in on the connection and per document; never sales. Each record carries the document name as its
external_ref, so a retry makes nothing twice and Koomyo's own events for it book nothing.
"""

import frappe

from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry
from koomyo_erpnext.sync import events
from koomyo_erpnext.tests.test_stock import KITCHEN, STORES, StockTestCase, adjustment, fixture
from koomyo_erpnext.tests.test_supplies_and_payments import SOAP

BRANCH = "90000000-0000-4000-8000-000000000001"
SPOILAGE = "Miscellaneous Expenses - _KTA"
MEALS = "Entertainment Expenses - _KTA"


class StockOutCase(StockTestCase):
	def setUp(self):
		super().setUp()
		self.fake_a.add_ingredient(SOAP, "Hand soap", "pcs", "50.0000")
		self.fake_a.ingredients[SOAP].update({"kind": "supply"})
		self.conn_a.reload()
		self.conn_a.push_stock_changes = 1
		self.conn_a.supplies_expense_account = "Miscellaneous Expenses - _KTA"
		self.conn_a.save()
		self.assertEqual(self.book(adjustment(1, "20.0000", unit_cost="12.5000")).status, "Done")
		self.assertEqual(
			self.book(adjustment(2, "20.0000", ingredient=SOAP, unit_cost="50.0000")).status, "Done"
		)
		self.fake_a.writes.clear()
		self.seq = 100

	def run_queue(self):
		with self.koomyo():
			events.process_pending(self.conn_a.name)

	def entry(self, purpose="Material Issue", rows=None, send_as=None, remarks=None):
		rows = rows or [(KITCHEN, 3)]
		doc = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"company": self.conn_a.company,
				"stock_entry_type": purpose,
				"purpose": purpose,
				"koomyo_send_as": send_as,
				"remarks": remarks,
				"items": [
					{
						"item_code": self.item(ingredient),
						"qty": qty,
						"s_warehouse": STORES if purpose == "Material Issue" else None,
						"t_warehouse": STORES if purpose == "Material Receipt" else None,
						"expense_account": SPOILAGE,
						"cost_center": self.conn_a.default_cost_center,
						"basic_rate": 12.5,
						"allow_zero_valuation_rate": 1,
					}
					for ingredient, qty in rows
				],
			}
		)
		doc.insert()
		doc.submit()
		frappe.db.commit()
		self.run_queue()
		return doc

	def logs_of(self, *types):
		return [
			l
			for l in self.logs(self.conn_a.name, received_via="ERPNext")
			if not types or l.event_type in types
		]

	def calls(self, path):
		return [body for p, body in self.fake_a.stock_calls if p == path]

	def sent(self, doc):
		return frappe.db.get_value("Stock Entry", doc.name, "koomyo_sent")

	def cancel(self, doc):
		frappe.get_doc("Stock Entry", doc.name).cancel()
		frappe.db.commit()
		self.run_queue()


class TestAdjustments(StockOutCase):
	def test_breakage_is_a_negative_adjustment_with_the_documents_name_as_its_reference(self):
		doc = self.entry(send_as="Adjustment", remarks="a tray fell")
		(log,) = self.logs_of("erpnext.stock_change")
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		(body,) = self.calls("/stock-adjustments")
		self.assertEqual(
			(body["place"], body["ingredient_id"], body["quantity"]),
			({"type": "branch", "id": BRANCH}, KITCHEN, "-3"),
		)
		self.assertEqual(body["reason"], f"ERPNext {doc.name}")
		self.assertEqual(body["note"], "a tray fell")
		self.assertEqual(body["external_ref"], {"system": "erpnext", "external_id": f"{doc.name}#1"})
		self.assertEqual(self.sent(doc), 1)
		row = frappe.get_doc("Stock Entry", doc.name).items[0]
		self.assertEqual(row.koomyo_line_id, "mv-1")

	def test_a_found_box_is_a_positive_adjustment(self):
		self.entry("Material Receipt", [(KITCHEN, 2)], send_as="Adjustment")
		self.assertEqual(self.calls("/stock-adjustments")[0]["quantity"], "2")

	def test_every_row_is_its_own_adjustment_and_a_replay_makes_none_more(self):
		doc = self.entry(rows=[(KITCHEN, 1), (SOAP, 2)], send_as="Adjustment")
		self.assertEqual(len(self.calls("/stock-adjustments")), 2)
		(log,) = self.logs_of("erpnext.stock_change")
		retry([log.name])
		self.run_queue()
		self.assertEqual(len(self.calls("/stock-adjustments")), 2)
		self.assertEqual(self.sent(doc), 1)

	def test_an_entry_that_is_not_marked_is_never_sent(self):
		self.entry()
		self.assertEqual(self.logs_of(), [])
		self.assertEqual(self.fake_a.stock_calls, [])

	def test_switched_off_sends_nothing(self):
		self.conn_a.reload()
		self.conn_a.push_stock_changes = 0
		self.conn_a.save()
		self.entry(send_as="Adjustment")
		self.assertEqual(self.logs_of(), [])

	def test_a_warehouse_that_is_not_on_the_branch_map_names_it(self):
		doc = self.entry(send_as="Adjustment")
		frappe.db.set_value("Stock Entry", doc.name, "koomyo_sent", 0)
		self.conn_a.reload()
		self.conn_a.branch_map = [r for r in self.conn_a.branch_map if r.warehouse != STORES]
		self.conn_a.save()
		other = frappe.get_doc("Stock Entry", doc.name)
		events.store_outbound(self.conn_a, "erpnext.stock_change", other, action="again")
		self.run_queue()
		(failed,) = [l for l in self.logs_of("erpnext.stock_change") if l.status == "Failed"]
		self.assertEqual(failed.error_class, "NeedsMapping")
		self.assertIn(STORES, failed.message)

	def test_cancelling_sends_the_opposite_adjustment(self):
		doc = self.entry(send_as="Adjustment")
		self.cancel(doc)
		(log,) = self.logs_of("erpnext.stock_change_cancel")
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		first, second = self.calls("/stock-adjustments")
		self.assertEqual((first["quantity"], second["quantity"]), ("-3", "3"))
		self.assertEqual(second["external_ref"]["external_id"], f"{doc.name}#1-cancel")

	def test_koomyos_event_for_it_books_nothing_by_reference_or_by_id(self):
		doc = self.entry(send_as="Adjustment")
		before = frappe.db.count("Stock Entry", {"company": self.conn_a.company})
		by_ref = adjustment(3, "-3.0000", movement_id="mv-x")
		by_ref["data"]["external_refs"] = {"erpnext": f"{doc.name}#1"}
		self.assertEqual(self.book(by_ref).status, "Skipped")
		by_id = adjustment(4, "-3.0000", movement_id="mv-1")
		self.assertEqual(self.book(by_id).status, "Skipped")
		self.assertEqual(frappe.db.count("Stock Entry", {"company": self.conn_a.company}), before)

	def test_a_movement_made_in_koomyo_is_still_booked(self):
		log = self.book(adjustment(5, "-1.0000", movement_id="koomyo-own"))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))

	def test_below_zero_is_a_warning_on_a_done_row(self):
		self.fake_a.below_zero = True
		self.entry(send_as="Adjustment")
		(log,) = self.logs_of("erpnext.stock_change")
		self.assertEqual(log.status, "Done")
		self.assertIn("below zero", log.message)


class TestWaste(StockOutCase):
	def test_the_reason_comes_from_the_waste_map_by_the_expense_account(self):
		doc = self.entry(rows=[(KITCHEN, 2)], send_as="Waste", remarks="went off")
		(log,) = self.logs_of("erpnext.stock_change")
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		(body,) = self.calls("/waste")
		self.assertEqual(body["reason"], "spoilage")
		self.assertEqual(body["place"], {"type": "branch", "id": BRANCH})
		self.assertEqual(body["note"], "went off")
		(line,) = body["lines"]
		self.assertEqual(
			(line["ingredient_id"], line["quantity"], line["external_ref"]["external_id"]),
			(KITCHEN, "2", f"{doc.name}#1"),
		)
		self.assertEqual(frappe.get_doc("Stock Entry", doc.name).items[0].koomyo_line_id, "waste-1")

	def test_an_account_that_is_not_on_the_waste_map_names_it(self):
		self.conn_a.reload()
		self.conn_a.waste_map = []
		self.conn_a.save()
		self.entry(send_as="Waste")
		(log,) = self.logs_of("erpnext.stock_change")
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn("Waste Map", log.message)
		self.assertEqual(self.fake_a.stock_calls, [])

	def test_a_supply_is_refused_as_waste_and_says_what_to_use(self):
		self.entry(rows=[(SOAP, 1)], send_as="Waste")
		(log,) = self.logs_of("erpnext.stock_change")
		self.assertEqual(log.status, "Failed")
		self.assertIn("Supply issue", log.message)

	def test_a_receipt_cannot_be_waste(self):
		self.entry("Material Receipt", send_as="Waste")
		(log,) = self.logs_of("erpnext.stock_change")
		self.assertEqual(log.status, "Failed")
		self.assertIn("only a Material Issue", log.message)

	def test_koomyos_waste_event_for_it_books_nothing(self):
		doc = self.entry(rows=[(KITCHEN, 2)], send_as="Waste")
		event = fixture("event_waste_recorded", 40)
		event["data"]["id"] = "waste-1"
		event["data"]["external_refs"] = {"erpnext": f"{doc.name}#1"}
		self.assertEqual(self.book(event).status, "Skipped")

	def test_waste_cannot_be_cancelled_in_erpnext_because_koomyo_cannot_undo_it(self):
		doc = self.entry(send_as="Waste")
		self.fake_a.stock_calls.clear()
		with self.assertRaises(frappe.ValidationError) as caught:
			frappe.get_doc("Stock Entry", doc.name).cancel()
		self.assertIn("no call to undo waste", str(caught.exception))
		self.assertEqual(frappe.db.get_value("Stock Entry", doc.name, "docstatus"), 1)
		self.assertEqual(self.fake_a.stock_calls, [])


class TestSupplyIssues(StockOutCase):
	def test_a_supply_issue_is_sent_and_undone_when_cancelled(self):
		doc = self.entry(rows=[(SOAP, 4)], send_as="Supply issue", remarks="cleaning")
		(log,) = self.logs_of("erpnext.stock_change")
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		(body,) = self.calls("/supply-issues")
		self.assertEqual((body["ingredient_id"], body["quantity"], body["reason"]), (SOAP, "4", "used"))
		self.assertEqual(body["external_ref"]["external_id"], f"{doc.name}#1")
		self.cancel(doc)
		(cancel,) = self.logs_of("erpnext.stock_change_cancel")
		self.assertEqual(cancel.status, "Done", (cancel.message, cancel.error[-300:]))
		self.assertTrue(self.fake_a.stock_records["sup-1"]["undone"])
		self.assertEqual(frappe.get_doc("Stock Entry", doc.name).items[0].koomyo_line_id, "sup-1 sup-1-undo")

	def test_an_issue_already_undone_in_koomyo_is_fine(self):
		doc = self.entry(rows=[(SOAP, 1)], send_as="Supply issue")
		self.fake_a.stock_records["sup-1"]["undone"] = True
		self.cancel(doc)
		(cancel,) = self.logs_of("erpnext.stock_change_cancel")
		self.assertEqual(cancel.status, "Done", (cancel.message, cancel.error[-300:]))

	def test_the_supplies_switch_off_in_koomyo_says_so(self):
		self.fake_a.supplies_on = False
		self.entry(rows=[(SOAP, 1)], send_as="Supply issue")
		(log,) = self.logs_of("erpnext.stock_change")
		self.assertEqual(log.status, "Failed")
		self.assertIn("Supplies switch is off", log.message)

	def test_a_food_item_is_not_a_supply(self):
		self.entry(rows=[(KITCHEN, 1)], send_as="Supply issue")
		(log,) = self.logs_of("erpnext.stock_change")
		self.assertEqual(log.status, "Failed")


class TestCounts(StockOutCase):
	def reconcile(self, qty, send=True):
		doc = frappe.get_doc(
			{
				"doctype": "Stock Reconciliation",
				"company": self.conn_a.company,
				"purpose": "Stock Reconciliation",
				"posting_date": "2026-10-20",
				"set_posting_time": 1,
				"expense_account": "Stock Adjustment - _KTA",
				"cost_center": self.conn_a.default_cost_center,
				"koomyo_send": 1 if send else 0,
				"items": [
					{
						"item_code": self.item(KITCHEN),
						"warehouse": STORES,
						"qty": qty,
						"valuation_rate": 12.5,
					}
				],
			}
		)
		doc.insert()
		doc.submit()
		frappe.db.commit()
		self.run_queue()
		return doc

	def test_a_marked_reconciliation_is_a_count_started_filled_and_closed(self):
		doc = self.reconcile(14)
		(log,) = self.logs_of("erpnext.stock_count")
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		paths = [p for p, _b in self.fake_a.stock_calls]
		self.assertEqual(
			paths, ["/stock-counts", "/stock-counts/count-1/lines", "/stock-counts/count-1/close"]
		)
		start = self.fake_a.stock_calls[0][1]
		self.assertEqual(start["place"], {"type": "branch", "id": BRANCH})
		self.assertEqual(start["external_ref"]["external_id"], f"{doc.name}#{STORES}")
		lines = self.fake_a.stock_calls[1][1]["lines"]
		self.assertEqual(lines, [{"ingredient_id": KITCHEN, "counted_quantity": "14"}])
		self.assertEqual(self.fake_a.stock_records["count-1"]["status"], "closed")
		self.assertEqual(frappe.db.get_value("Stock Reconciliation", doc.name, "koomyo_sent"), 1)

	def test_an_unmarked_reconciliation_is_not_sent(self):
		self.reconcile(14, send=False)
		self.assertEqual(self.logs_of(), [])

	def test_a_replay_after_the_count_closed_changes_nothing(self):
		self.reconcile(14)
		(log,) = self.logs_of("erpnext.stock_count")
		retry([log.name])
		self.run_queue()
		self.assertEqual(len(self.fake_a.stock_records), 1)
		self.assertEqual(len([p for p, _b in self.fake_a.stock_calls if p.endswith("/close")]), 1)

	def test_a_failure_after_the_start_resumes_the_same_count(self):
		from koomyo_erpnext.tests.fake_koomyo import error_response

		self.fake_a.scripted["/stock-counts/count-1/lines"] = [
			error_response(400, "invalid_parameter", "try again")
		]
		self.reconcile(14)
		(log,) = self.logs_of("erpnext.stock_count")
		self.assertEqual(log.status, "Failed")
		retry([log.name])
		self.run_queue()
		self.assertEqual(len(self.fake_a.stock_records), 1, "the same count, not a second one")
		self.assertEqual(self.fake_a.stock_records["count-1"]["status"], "closed")

	def test_koomyos_closed_event_for_it_books_nothing(self):
		doc = self.reconcile(14)
		event = fixture("event_stock_count_closed", 41)
		event["data"]["id"] = "count-1"
		event["data"]["external_refs"] = {"erpnext": f"{doc.name}#{STORES}"}
		self.assertEqual(self.book(event).status, "Skipped")

	def test_a_closed_count_cannot_be_cancelled_in_erpnext_because_koomyo_locks_it(self):
		doc = self.reconcile(14)
		with self.assertRaises(frappe.ValidationError) as caught:
			frappe.get_doc("Stock Reconciliation", doc.name).cancel()
		self.assertIn("closed count is locked", str(caught.exception))
		self.assertEqual(frappe.db.get_value("Stock Reconciliation", doc.name, "docstatus"), 1)
