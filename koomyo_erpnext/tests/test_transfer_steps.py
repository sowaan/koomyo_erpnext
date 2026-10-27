"""Section 4: stock transfers in steps (sent, each delivery, returned or lost, cancelled).

The docs example is TR-2026-0017: 10 kg of mozzarella from the central warehouse to a branch, 7 kg
arrive, 3 kg are written off as damaged. Quantities are read from the Bin; values from the GL.
"""

import copy
from decimal import Decimal as D

import frappe

from koomyo_erpnext.sync import transfers
from koomyo_erpnext.tests.fake_koomyo import load_fixture
from koomyo_erpnext.tests.helpers import CENTRAL_WAREHOUSE, TENANT_A, ensure_company, gl
from koomyo_erpnext.tests.test_stock import (
	CENTRAL,
	MOZZARELLA,
	STORES,
	StockTestCase,
	adjustment,
)

BRANCH = "90000000-0000-4000-8000-000000000001"
TRANSIT = "Koomyo Transit - _KTA"
TRANSFER_ID = "bfd20268-eda2-4549-95ea-ea7b7158e132"
LINE_ID = "5a2c8f1e-3b7d-4c9a-9e61-0f4d2b7a1c10"
STEPS = "event_stock_transfer_completed_steps"


def receipt(rid, qty, at="2026-10-12T17:11:40Z"):
	return {
		"id": rid,
		"received_at": at,
		"note": None,
		"lines": [
			{"line_id": LINE_ID, "ingredient_id": MOZZARELLA, "quantity": qty, "unit_cost": "1450.0000"}
		],
	}


def resolution(rid, kind, qty, reason="damaged", at="2026-10-12T17:30:00Z"):
	return {
		"id": rid,
		"line_id": LINE_ID,
		"ingredient_id": MOZZARELLA,
		"kind": kind,
		"quantity": qty,
		"unit_cost": "1450.0000",
		"value": str(D(qty) * D("1450")),
		"reason_code": reason if kind == "lost" else None,
		"note": "Crushed in the van",
		"resolved_at": at,
	}


def transfer(status="in_transit", receipts=(), resolutions=(), sent="10.000", **changes):
	data = copy.deepcopy(load_fixture(STEPS)["data"])
	got = sum((D(r["lines"][0]["quantity"]) for r in receipts), D(0))
	back = sum((D(r["quantity"]) for r in resolutions if r["kind"] == "returned"), D(0))
	lost = sum((D(r["quantity"]) for r in resolutions if r["kind"] == "lost"), D(0))
	data["lines"][0].update(
		{
			"quantity_sent": sent,
			"quantity_received": str(got),
			"quantity_returned": str(back),
			"quantity_lost": str(lost),
			"quantity_open": str(D(sent) - got - back - lost),
		}
	)
	data.update({"status": status, "receipts": list(receipts), "resolutions": list(resolutions), **changes})
	return data


def event(event_type, data, seq, eid=None):
	ev = copy.deepcopy(load_fixture(STEPS))
	ev.update(
		{"type": event_type, "tenant_id": TENANT_A, "seq": seq, "id": eid or f"evt-tr-{seq}", "data": data}
	)
	ev["entity"] = {"id": data["id"], "type": "stock_transfer"}
	if event_type == "transfer.received":
		ev["entity"] = {"id": data["receipts"][-1]["id"], "type": "stock_transfer_receipt"}
		ev["data"]["receipt"] = data["receipts"][-1]
	return ev


class TransferStepsCase(StockTestCase):
	def setUp(self):
		super().setUp()
		if not frappe.db.exists("Warehouse Type", "Transit"):
			frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert()
		if not frappe.db.exists("Warehouse", TRANSIT):
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": "Koomyo Transit",
					"company": self.conn_a.company,
					"warehouse_type": "Transit",
					"parent_warehouse": "All Warehouses - _KTA",
				}
			).insert()
		self.conn_a.reload()
		self.conn_a.update({"transit_warehouse": TRANSIT})
		self.conn_a.save()
		self.seq = 100
		self.assertEqual(
			self.book(
				adjustment(
					1,
					"10.0000",
					ingredient=MOZZARELLA,
					location={"type": "warehouse", "id": CENTRAL_WAREHOUSE},
					unit_cost="1450.0000",
				)
			).status,
			"Done",
		)

	def send(self, event_type, data, eid=None):
		self.seq += 1
		return self.book(event(event_type, data, self.seq, eid))

	def stock(self):
		return tuple(self.qty(w, MOZZARELLA) for w in (CENTRAL, TRANSIT, STORES))

	def entries(self, like="transfer:%"):
		return frappe.get_all(
			"Stock Entry",
			filters={"koomyo_connection": self.conn_a.name, "koomyo_id": ("like", like), "docstatus": 1},
			fields=["name", "koomyo_id", "purpose", "add_to_transit", "outgoing_stock_entry"],
			order_by="creation",
		)


class TestSteps(TransferStepsCase):
	def test_sent_takes_the_stock_into_transit(self):
		log = self.send("transfer.sent", transfer())
		self.assertEqual(log.status, "Done", (log.message, log.error[-600:]))
		self.assertEqual(self.stock(), (D(0), D(10), D(0)))
		(send,) = self.entries()
		self.assertEqual((send.purpose, send.add_to_transit), ("Material Transfer", 1))
		self.assertEqual(send.koomyo_id, f"transfer:{TRANSFER_ID}:send")
		# the same event again, or the same transfer from another event, changes nothing
		self.send("transfer.sent", transfer(), eid="again")
		self.assertEqual(len(self.entries()), 1)

	def test_each_delivery_posts_once_and_the_loss_is_a_write_off(self):
		self.send("transfer.sent", transfer())
		first = transfer("partial", receipts=[receipt("rcpt-1", "7.000")])
		log = self.send("transfer.received", first)
		self.assertEqual(log.status, "Done", (log.message, log.error[-600:]))
		self.assertEqual(self.stock(), (D(0), D(3), D(7)))
		recv = [e for e in self.entries() if e.koomyo_id.endswith("recv:rcpt-1")]
		self.assertEqual(len(recv), 1)
		self.assertTrue(recv[0].outgoing_stock_entry)

		closed = transfer(
			"received",
			receipts=[receipt("rcpt-1", "7.000")],
			resolutions=[resolution("res-1", "lost", "3.000")],
		)
		log = self.send("transfer.short_closed", closed)
		self.assertEqual(log.status, "Done", (log.message, log.error[-600:]))
		self.assertEqual(self.stock(), (D(0), D(0), D(7)))
		loss = frappe.db.get_value(
			"Stock Entry",
			{"koomyo_connection": self.conn_a.name, "koomyo_id": f"transfer:{TRANSFER_ID}:res:res-1"},
		)
		self.assertEqual(frappe.db.get_value("Stock Entry", loss, "purpose"), "Material Issue")
		self.assertEqual(gl("Stock Entry", loss)["Miscellaneous Expenses - _KTA"], D("4350.00"))
		self.assertIn("damaged", frappe.db.get_value("Stock Entry", loss, "remarks"))

		# the old completed event, the same events again: nothing more is posted
		count = len(self.entries())
		self.send("stock_transfer.completed", closed)
		self.send("transfer.short_closed", closed, eid="again")
		self.send("transfer.received", first, eid="again2")
		self.assertEqual(len(self.entries()), count)
		self.assertEqual(self.stock(), (D(0), D(0), D(7)))

	def test_several_deliveries_are_each_their_own_entry(self):
		self.send("transfer.sent", transfer())
		r1, r2 = receipt("rcpt-1", "4.000"), receipt("rcpt-2", "6.000", at="2026-10-13T08:00:00Z")
		self.send("transfer.received", transfer("partial", receipts=[r1]))
		self.send("transfer.received", transfer("received", receipts=[r1, r2]))
		self.assertEqual(self.stock(), (D(0), D(0), D(10)))
		self.assertEqual(
			sorted(e.koomyo_id.split(":")[-1] for e in self.entries() if ":recv:" in e.koomyo_id),
			["rcpt-1", "rcpt-2"],
		)

	def test_returned_goes_back_to_the_source(self):
		self.send("transfer.sent", transfer())
		closed = transfer(
			"received",
			receipts=[receipt("rcpt-1", "7.000")],
			resolutions=[resolution("res-r", "returned", "3.000")],
			closed_short=True,
		)
		log = self.send("transfer.short_closed", closed)
		self.assertEqual(log.status, "Done", (log.message, log.error[-600:]))
		self.assertEqual(self.stock(), (D(3), D(0), D(7)))

	def test_events_in_any_order_make_the_same_entries(self):
		"""The delivery arrives before the send was ever seen: the whole transfer is posted once."""
		closed = transfer(
			"received",
			receipts=[receipt("rcpt-1", "7.000")],
			resolutions=[resolution("res-1", "lost", "3.000")],
		)
		self.assertEqual(self.send("transfer.short_closed", closed).status, "Done")
		self.assertEqual(self.stock(), (D(0), D(0), D(7)))
		count = len(self.entries())
		self.send("transfer.sent", transfer(), eid="late-sent")
		self.send(
			"transfer.received", transfer("partial", receipts=[receipt("rcpt-1", "7.000")]), eid="late-recv"
		)
		self.assertEqual(len(self.entries()), count)

	def test_completed_alone_posts_the_whole_transfer(self):
		closed = transfer(
			"received",
			receipts=[receipt("rcpt-1", "7.000")],
			resolutions=[resolution("res-1", "lost", "3.000")],
		)
		log = self.send("stock_transfer.completed", closed)
		self.assertEqual(log.status, "Done", (log.message, log.error[-600:]))
		self.assertEqual(self.stock(), (D(0), D(0), D(7)))
		self.assertEqual(len(self.entries()), 3, "send, one delivery, one write-off")

	def test_the_old_shape_without_receipts_still_works_with_a_transit_warehouse(self):
		data = transfer("received", receipts=[], resolutions=[])
		data["lines"][0].update({"quantity_received": "7.000", "quantity_open": "3.000"})
		log = self.send("stock_transfer.completed", data)
		self.assertEqual(log.status, "Done", (log.message, log.error[-600:]))
		self.assertEqual(self.stock(), (D(0), D(0), D(7)))
		loss = [e for e in self.entries() if e.purpose == "Material Issue"]
		self.assertEqual(len(loss), 1, "what did not arrive is the loss, as before")

	def test_draft_creates_nothing(self):
		log = self.send("transfer.sent", transfer("draft"))
		self.assertEqual(log.status, "Skipped")
		self.assertEqual(self.entries(), [])

	def test_more_than_is_in_transit_is_refused(self):
		self.send("transfer.sent", transfer(sent="5.000"))
		bad = transfer("partial", receipts=[receipt("rcpt-1", "7.000")], sent="5.000")
		log = self.send("transfer.received", bad)
		self.assertEqual(log.status, "Failed")
		self.assertEqual(self.stock(), (D(5), D(5), D(0)), "nothing of the bad delivery was posted")

	def test_a_loss_needs_its_account(self):
		self.conn_a.reload()
		self.conn_a.transfer_loss_account = None
		self.conn_a.save()
		self.send("transfer.sent", transfer())
		closed = transfer(
			"received",
			receipts=[receipt("rcpt-1", "7.000")],
			resolutions=[resolution("res-1", "lost", "3.000")],
		)
		log = self.send("transfer.short_closed", closed)
		self.assertEqual(log.error_class, "NeedsMapping")
		self.assertIn("Transfer Loss Account", log.message)


class TestCancel(TransferStepsCase):
	def test_cancel_gives_the_source_its_stock_back(self):
		self.send("transfer.sent", transfer())
		log = self.send("transfer.cancelled", transfer("cancelled", cancel_reason="Wrong branch"))
		self.assertEqual(log.status, "Done", (log.message, log.error[-600:]))
		self.assertEqual(self.stock(), (D(10), D(0), D(0)))
		self.send("transfer.cancelled", transfer("cancelled"), eid="again")
		self.assertEqual(self.stock(), (D(10), D(0), D(0)))

	def test_cancel_after_a_delivery_is_flagged_not_forced(self):
		self.send("transfer.sent", transfer())
		self.send("transfer.received", transfer("partial", receipts=[receipt("rcpt-1", "7.000")]))
		log = self.send("transfer.cancelled", transfer("cancelled"))
		self.assertEqual(log.status, "Failed")
		self.assertIn("already booked", log.message)
		self.assertEqual(self.stock(), (D(0), D(3), D(7)))

	def test_cancel_before_the_send_was_seen_blocks_a_late_send(self):
		log = self.send("transfer.cancelled", transfer("cancelled"))
		self.assertEqual(log.status, "Skipped")
		late = self.send("transfer.sent", transfer(), eid="late")
		self.assertEqual(late.status, "Skipped")
		self.assertEqual(self.stock(), (D(10), D(0), D(0)))


class TestWithoutTransit(TransferStepsCase):
	def test_steps_are_skipped_and_completed_books_in_one_step(self):
		self.conn_a.reload()
		self.conn_a.transit_warehouse = None
		self.conn_a.save()
		log = self.send("transfer.sent", transfer())
		self.assertEqual(log.status, "Skipped")
		self.assertIn("Transit Warehouse", log.message)
		self.assertEqual(self.stock(), (D(10), D(0), D(0)))
		closed = transfer(
			"received",
			receipts=[receipt("rcpt-1", "7.000")],
			resolutions=[resolution("res-1", "lost", "3.000")],
		)
		self.assertEqual(self.send("stock_transfer.completed", closed).status, "Done")
		self.assertEqual(self.stock(), (D(0), D(0), D(7)))

	def test_transit_must_belong_to_the_company_and_not_be_a_branch_warehouse(self):
		ensure_company("_Koomyo Test B", "_KTB")
		self.conn_a.reload()
		self.conn_a.transit_warehouse = "Stores - _KTB"
		self.assertRaises(frappe.ValidationError, self.conn_a.save)
		self.conn_a.reload()
		self.conn_a.transit_warehouse = STORES
		self.assertRaises(frappe.ValidationError, self.conn_a.save)


class TestBackfillCount(TransferStepsCase):
	def test_fully_booked_follows_the_steps(self):
		sent = transfer()
		self.assertFalse(transfers.fully_booked(sent, self.conn_a))
		self.send("transfer.sent", sent)
		self.assertTrue(transfers.fully_booked(sent, self.conn_a))
		closed = transfer("received", receipts=[receipt("rcpt-1", "10.000")])
		self.assertFalse(transfers.fully_booked(closed, self.conn_a))
		self.send("transfer.received", closed)
		self.assertTrue(transfers.fully_booked(closed, self.conn_a))
