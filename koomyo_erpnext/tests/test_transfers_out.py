"""Item 6: ERPNext Material Transfers become Koomyo stock transfers (POST /stock-transfers, receive, resolve, cancel).

Opt in per connection. A plain transfer is sent and received at once; in steps (Transit Warehouse) the first
Receive at Warehouse makes the transfer in Koomyo (ERPNext only knows the destination then). Koomyo's own
events for what ERPNext made book nothing twice.
"""

import frappe

from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry
from koomyo_erpnext.sync import events
from koomyo_erpnext.tests.fake_koomyo import error_response
from koomyo_erpnext.tests.helpers import CENTRAL_WAREHOUSE
from koomyo_erpnext.tests.test_stock import CENTRAL, MOZZARELLA, STORES
from koomyo_erpnext.tests.test_transfer_steps import (
	BRANCH,
	TRANSIT,
	TransferStepsCase,
)
from koomyo_erpnext.tests.test_transfer_steps import (
	transfer as transfer_data,
)


class TransfersOutCase(TransferStepsCase):
	def setUp(self):
		super().setUp()
		self.conn_a.reload()
		self.conn_a.push_stock_transfers = 1
		self.conn_a.save()
		self.fake_a.writes.clear()
		self.baseline = set(self.all_entries())

	def run_queue(self):
		with self.koomyo():
			events.process_pending(self.conn_a.name)

	def make_entry(self, source, target, qty, transit=False, submit=True):
		entry = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"company": self.conn_a.company,
				"stock_entry_type": "Material Transfer",
				"purpose": "Material Transfer",
				"add_to_transit": 1 if transit else 0,
				"from_warehouse": source,
				"to_warehouse": target,
				"items": [
					{
						"item_code": self.item(MOZZARELLA),
						"qty": qty,
						"s_warehouse": source,
						"t_warehouse": target,
					}
				],
			}
		)
		entry.insert()
		if submit:
			entry.submit()
		frappe.db.commit()
		return entry

	def receive_entry(self, sent, qty, target):
		from erpnext.stock.doctype.stock_entry.stock_entry import make_stock_in_entry

		entry = make_stock_in_entry(sent.name)
		for row in entry.items:
			row.qty = qty
			row.t_warehouse = target
		entry.insert()
		entry.submit()
		frappe.db.commit()
		return entry

	def outbound_logs(self, *types):
		return [
			l
			for l in self.logs(self.conn_a.name, received_via="ERPNext")
			if not types or l.event_type in types
		]

	def transfers(self):
		return list(self.fake_a.transfers.values())

	def all_entries(self):
		return frappe.get_all(
			"Stock Entry",
			filters={"company": self.conn_a.company, "purpose": "Material Transfer", "docstatus": 1},
			pluck="name",
		)

	def entries(self):
		"""The Material Transfers made since this test began."""
		return [n for n in self.all_entries() if n not in self.baseline]


class TestOneStep(TransfersOutCase):
	def test_a_plain_transfer_is_sent_and_received_at_once(self):
		entry = self.make_entry(CENTRAL, STORES, 4)
		self.run_queue()
		(log,) = self.outbound_logs("erpnext.stock_transfer")
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		(transfer,) = self.transfers()
		self.assertEqual(transfer["from"]["id"], CENTRAL_WAREHOUSE)
		self.assertEqual((transfer["to"]["type"], transfer["to"]["id"]), ("branch", BRANCH))
		self.assertEqual(
			[(l["ingredient_id"], l["quantity_sent"]) for l in transfer["lines"]], [(MOZZARELLA, "4.000")]
		)
		self.assertEqual(transfer["reference"], entry.name)
		self.assertEqual(transfer["external_refs"], {"erpnext": entry.name})
		self.assertEqual((transfer["status"], len(transfer["receipts"])), ("received", 1))
		row = frappe.db.get_value("Stock Entry", entry.name, ["koomyo_id", "koomyo_sent"], as_dict=True)
		self.assertEqual((row.koomyo_id, row.koomyo_sent), (transfer["id"], 1))

	def test_koomyos_own_events_for_it_book_nothing(self):
		entry = self.make_entry(CENTRAL, STORES, 4)
		self.run_queue()
		(transfer,) = self.transfers()
		self.assertEqual(self.send("transfer.sent", dict(transfer)).status, "Done")
		got = self.send("transfer.received", dict(transfer))
		self.assertIn(got.status, ("Done", "Skipped"), got.message)
		self.assertEqual(self.entries(), [entry.name], "no second entry")
		self.assertEqual(len(self.outbound_logs()), 1, "nothing is sent back for Koomyo's own events")

	def test_an_event_that_arrives_before_the_answer_adopts_the_entry(self):
		entry = self.make_entry(CENTRAL, STORES, 4)
		self.run_queue()
		(transfer,) = self.transfers()
		frappe.db.set_value(
			"Stock Entry", entry.name, {"koomyo_connection": None, "koomyo_id": None, "koomyo_sent": 0}
		)
		frappe.db.commit()
		self.send("transfer.sent", dict(transfer))
		self.assertEqual(self.entries(), [entry.name], "no second entry")
		self.assertEqual(frappe.db.get_value("Stock Entry", entry.name, "koomyo_id"), transfer["id"])

	def test_a_replay_sends_nothing_twice(self):
		self.make_entry(CENTRAL, STORES, 4)
		self.run_queue()
		(log,) = self.outbound_logs("erpnext.stock_transfer")
		retry([log.name])
		self.run_queue()
		self.assertEqual(len(self.transfers()), 1)
		self.assertEqual(len(self.fake_a.transfers["tr-1"]["receipts"]), 1)

	def test_a_receive_that_fails_is_resumed_without_a_second_transfer(self):
		self.make_entry(CENTRAL, STORES, 4)
		self.fake_a.scripted["/stock-transfers/tr-1/receive"] = [
			error_response(400, "invalid_parameter", "try later")
		]
		self.run_queue()
		(log,) = self.outbound_logs("erpnext.stock_transfer")
		self.assertNotEqual(log.status, "Done")
		self.assertEqual(len(self.transfers()), 1)
		retry([log.name])
		self.run_queue()
		self.assertEqual(len(self.transfers()), 1)
		self.assertEqual(self.transfers()[0]["status"], "received")

	def test_an_id_koomyo_does_not_know_as_an_ingredient_names_the_item(self):
		from koomyo_erpnext.tests.test_stock import MOZZARELLA as ING

		del self.fake_a.ingredients[ING]  # as when the Item is linked to a menu item's id
		self.make_entry(CENTRAL, STORES, 4)
		self.run_queue()
		(log,) = self.outbound_logs("erpnext.stock_transfer")
		self.assertEqual(log.status, "Failed")
		self.assertIn("probably a menu item", log.message)
		self.assertEqual(self.transfers(), [])

	def test_switched_off_sends_nothing(self):
		self.conn_a.reload()
		self.conn_a.push_stock_transfers = 0
		self.conn_a.save()
		self.make_entry(CENTRAL, STORES, 1)
		self.run_queue()
		self.assertEqual(self.outbound_logs(), [])
		self.assertEqual(self.transfers(), [])

	def test_one_side_unmapped_names_the_warehouse(self):
		self.make_entry(CENTRAL, "Work In Progress - _KTA", 1)
		self.run_queue()
		(log,) = self.outbound_logs("erpnext.stock_transfer")
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn("Work In Progress - _KTA", log.message)
		self.assertEqual(self.transfers(), [])

	def test_a_draft_moves_nothing_and_sends_nothing(self):
		self.make_entry(CENTRAL, STORES, 2, submit=False)
		self.run_queue()
		self.assertEqual(self.outbound_logs(), [])
		self.assertEqual(self.transfers(), [])

	def test_a_transfer_made_from_koomyo_is_not_sent_back(self):
		data = transfer_data(status="in_transit", sent="3.000")
		self.assertEqual(self.send("transfer.sent", data).status, "Done")
		self.run_queue()
		self.assertEqual(self.outbound_logs(), [])
		self.assertEqual(self.transfers(), [])


class TestSendIntoTheTransitWarehouse(TransfersOutCase):
	def test_a_send_into_a_real_warehouse_is_refused_at_submit_with_what_to_do(self):
		entry = self.make_entry(CENTRAL, STORES, 5, transit=True, submit=False)
		with self.assertRaises(frappe.ValidationError) as caught:
			entry.submit()
		message = str(caught.exception)
		self.assertIn(STORES, message)
		self.assertIn(TRANSIT, message)
		self.assertIn("through the Transit Warehouse", message)
		self.assertEqual(frappe.db.get_value("Stock Entry", entry.name, "docstatus"), 0)

	def test_a_send_into_the_transit_warehouse_is_fine(self):
		entry = self.make_entry(CENTRAL, TRANSIT, 5, transit=True)
		self.assertEqual(frappe.db.get_value("Stock Entry", entry.name, "docstatus"), 1)

	def test_without_a_transit_warehouse_on_the_connection_the_send_says_so(self):
		self.conn_a.reload()
		self.conn_a.transit_warehouse = None
		self.conn_a.save()
		entry = self.make_entry(CENTRAL, TRANSIT, 5, transit=True, submit=False)
		with self.assertRaises(frappe.ValidationError) as caught:
			entry.submit()
		self.assertIn("has no Transit Warehouse", str(caught.exception))

	def test_the_message_at_the_push_is_readable_when_a_send_slipped_through(self):
		sent = self.make_entry(CENTRAL, TRANSIT, 10, transit=True)
		frappe.db.set_value("Stock Entry Detail", {"parent": sent.name}, "t_warehouse", STORES)
		frappe.db.commit()
		self.receive_entry_unchecked(sent)
		self.run_queue()
		(log,) = self.outbound_logs("erpnext.stock_transfer")
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn(f"{sent.name} sent the stock into {STORES}", log.message)
		self.assertIn(TRANSIT, log.message)

	def receive_entry_unchecked(self, sent):
		from erpnext.stock.doctype.stock_entry.stock_entry import make_stock_in_entry

		entry = make_stock_in_entry(sent.name)
		for row in entry.items:
			row.qty = 5
			row.t_warehouse = STORES
		entry.insert()
		frappe.db.set_value("Stock Entry", entry.name, "docstatus", 1)  # as if it had been submitted earlier
		frappe.get_doc("Stock Entry", entry.name).run_method("on_submit")
		frappe.db.commit()


class TestInSteps(TransfersOutCase):
	def setUp(self):
		super().setUp()
		self.sent = self.make_entry(CENTRAL, TRANSIT, 10, transit=True)

	def test_the_send_alone_tells_koomyo_nothing(self):
		self.run_queue()
		self.assertEqual(self.outbound_logs(), [])
		self.assertEqual(self.transfers(), [])

	def test_the_first_receive_makes_the_transfer_with_what_was_sent_and_receives_what_arrived(self):
		got = self.receive_entry(self.sent, 7, STORES)
		self.run_queue()
		(log,) = self.outbound_logs("erpnext.stock_transfer")
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		(transfer,) = self.transfers()
		self.assertEqual(transfer["lines"][0]["quantity_sent"], "10.000")
		self.assertEqual(transfer["lines"][0]["quantity_received"], "7.000")
		self.assertEqual((transfer["status"], transfer["lines"][0]["quantity_open"]), ("partial", "3.000"))
		self.assertEqual(transfer["reference"], self.sent.name)
		self.assertEqual(
			frappe.db.get_value("Stock Entry", self.sent.name, "koomyo_id"), f"transfer:{transfer['id']}:send"
		)
		self.assertEqual(
			frappe.db.get_value("Stock Entry", got.name, "koomyo_id"),
			f"transfer:{transfer['id']}:recv:{transfer['receipts'][0]['id']}",
		)
		self.assertEqual(transfer["receipts"][0]["note"], f"ERPNext {got.name}")

	def test_a_later_receive_is_a_later_delivery_of_the_same_transfer(self):
		self.receive_entry(self.sent, 7, STORES)
		self.run_queue()
		self.receive_entry(self.sent, 3, STORES)
		self.run_queue()
		(transfer,) = self.transfers()
		self.assertEqual((transfer["status"], len(transfer["receipts"])), ("received", 2))

	def test_koomyos_events_book_nothing_and_a_loss_decided_there_is_booked(self):
		self.receive_entry(self.sent, 7, STORES)
		self.run_queue()
		(transfer,) = self.transfers()
		before = set(self.entries())
		self.assertEqual(self.send("transfer.sent", dict(transfer)).status, "Done")
		self.assertEqual(self.send("transfer.received", dict(transfer)).status, "Done")
		self.assertEqual(set(self.entries()), before, "the send and the delivery are ERPNext's own")
		# staff write off the 3 kg that never came, in Koomyo
		line = transfer["lines"][0]["id"]
		self.fake_a._transfer_action(
			transfer["id"],
			"resolve",
			{"resolutions": [{"line_id": line, "kind": "lost", "reason_code": "damaged"}]},
		)
		lost = self.send("transfer.short_closed", dict(self.fake_a.transfers[transfer["id"]]))
		self.assertEqual(lost.status, "Done", (lost.message, lost.error[-300:]))
		issues = frappe.get_all(
			"Stock Entry",
			filters={
				"company": self.conn_a.company,
				"purpose": "Material Issue",
				"koomyo_connection": self.conn_a.name,
			},
		)
		self.assertEqual(len(issues), 1)

	def test_stock_put_back_where_it_was_sent_from_is_a_return(self):
		self.receive_entry(self.sent, 6, STORES)
		self.run_queue()
		back = self.receive_entry(self.sent, 4, CENTRAL)
		self.run_queue()
		log = self.outbound_logs("erpnext.stock_transfer")[-1]
		self.assertEqual(log.status, "Done", (log.message, log.error[-400:]))
		(transfer,) = self.transfers()
		self.assertEqual(
			[(r["kind"], r["quantity"]) for r in transfer["resolutions"]], [("returned", "4.000")]
		)
		self.assertEqual(transfer["resolutions"][0]["note"], f"ERPNext {back.name}")
		self.assertEqual(transfer["status"], "received")

	def test_a_receive_for_another_destination_is_refused_with_a_message(self):
		self.receive_entry(self.sent, 5, STORES)
		self.run_queue()
		self.fake_a.transfers["tr-1"]["to"] = {"type": "branch", "id": "other-branch", "name": "Other"}
		self.receive_entry(self.sent, 2, STORES)
		self.run_queue()
		failed = [l for l in self.outbound_logs("erpnext.stock_transfer") if l.status == "Failed"]
		self.assertEqual(len(failed), 1)
		self.assertIn("has one destination", failed[0].message)

	def test_a_receive_back_before_anything_arrived_has_nothing_to_tell(self):
		self.receive_entry(self.sent, 10, CENTRAL)
		self.run_queue()
		(log,) = self.outbound_logs("erpnext.stock_transfer")
		self.assertEqual(log.status, "Skipped")
		self.assertIn("never knew this transfer", log.message)

	def test_the_transit_warehouse_must_be_the_one_the_stock_was_sent_to(self):
		self.conn_a.reload()
		self.conn_a.transit_warehouse = None
		self.conn_a.save()
		self.receive_entry(self.sent, 5, STORES)
		self.run_queue()
		(log,) = self.outbound_logs("erpnext.stock_transfer")
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn("Transit Warehouse", log.message)


class TestCancel(TransfersOutCase):
	def test_a_transfer_nothing_arrived_from_is_cancelled_in_koomyo_with_a_reason(self):
		entry = self.make_entry(CENTRAL, STORES, 4)
		self.run_queue()
		# Koomyo's copy was sent but nothing arrived (as when the receive step had failed)
		transfer = self.fake_a.transfers["tr-1"]
		transfer["receipts"].clear()
		transfer["lines"][0].update({"quantity_received": "0.000", "quantity_open": "4.000"})
		transfer["status"] = "in_transit"
		frappe.get_doc("Stock Entry", entry.name).cancel()
		frappe.db.commit()
		self.run_queue()
		(log,) = self.outbound_logs("erpnext.stock_transfer_cancel")
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertEqual(
			(transfer["status"], transfer["cancel_reason"]),
			("cancelled", f"Cancelled in ERPNext ({entry.name})"),
		)

	def test_once_goods_arrived_koomyo_cannot_cancel_and_says_what_to_do(self):
		entry = self.make_entry(CENTRAL, STORES, 4)
		self.run_queue()
		frappe.get_doc("Stock Entry", entry.name).cancel()
		frappe.db.commit()
		self.run_queue()
		(log,) = self.outbound_logs("erpnext.stock_transfer_cancel")
		self.assertEqual(log.status, "Failed")
		self.assertIn("goods already arrived", log.message)
		self.assertEqual(self.fake_a.transfers["tr-1"]["status"], "received")

	def test_cancelling_a_delivery_only_says_koomyo_cannot_undo_it(self):
		sent = self.make_entry(CENTRAL, TRANSIT, 10, transit=True)
		got = self.receive_entry(sent, 5, STORES)
		self.run_queue()
		self.fake_a.transfer_calls.clear()
		frappe.get_doc("Stock Entry", got.name).cancel()
		frappe.db.commit()
		self.run_queue()
		(log,) = self.outbound_logs("erpnext.stock_transfer_cancel")
		self.assertEqual(log.status, "Skipped")
		self.assertIn("no call to undo a delivery", log.message)
		self.assertEqual(self.fake_a.transfer_calls, [])

	def test_a_transfer_koomyo_cancelled_is_not_cancelled_back(self):
		sent = self.make_entry(CENTRAL, TRANSIT, 10, transit=True)
		made = self.fake_a._transfer_create(
			{
				"from": {"type": "warehouse", "id": CENTRAL_WAREHOUSE},
				"to": {"type": "branch", "id": BRANCH},
				"lines": [{"ingredient_id": MOZZARELLA, "quantity": "10"}],
				"reference": sent.name,
			}
		).json()
		frappe.db.set_value(
			"Stock Entry",
			sent.name,
			{
				"koomyo_connection": self.conn_a.name,
				"koomyo_id": f"transfer:{made['id']}:send",
				"koomyo_sent": 1,
			},
		)
		frappe.db.commit()
		log = self.send("transfer.cancelled", dict(made, status="cancelled", cancel_reason="by staff"))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertEqual(frappe.db.get_value("Stock Entry", sent.name, "docstatus"), 2)
		self.run_queue()
		self.assertEqual(self.outbound_logs("erpnext.stock_transfer_cancel"), [])
