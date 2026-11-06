"""Sales channels: orders punched in for a partner (Foodpanda, Careem), their commission, refunds and payouts.

Figures are the ones the Koomyo developer gave from the local server (ORD-TR-20261106-0020: total 636.84, commission
192.15, tax on it 30.74, net 413.95; Careem 1490.40 with a 100.00 refund; a recorded payout with two adjustments).
Every money check is an exact Decimal to the cent. Nothing here is ever written to Koomyo.
"""

import copy
from decimal import Decimal as D

import frappe

from koomyo_erpnext.sync import channels
from koomyo_erpnext.tests.fake_koomyo import load_fixture
from koomyo_erpnext.tests.helpers import (
	COMPANY_A,
	COMPANY_B,
	TENANT_A,
	TENANT_B,
	configure_sales,
	ensure_account,
	gl,
)
from koomyo_erpnext.tests.test_sales import SalesTestCase, order_event, refund_event

FOODPANDA = {"id": "79206157-b3f2-4ef7-9733-04ee733a5f02", "code": "foodpanda", "name": "Foodpanda"}
CAREEM = {"id": "da35616d-a743-455d-9ecc-57bf8e07bd13", "code": "careem", "name": "Careem"}


def channel_order(
	channel, order_id, total, commission, tax, net, seq=10, subtotal=None, tax_amount="0.00", **more
):
	"""A paid order of a channel; commission, tax and net are text (None for an order paid before they were recorded)."""
	subtotal = subtotal or str(D(total) - D(tax_amount))
	event = order_event(
		seq=seq,
		order_id=order_id,
		lines=[{"quantity": 1, "unit_price": subtotal, "line_total": subtotal}],
		payments=[(f"ch_{channel['code']}", total)],
		items_subtotal=subtotal,
		tax=tax_amount,
		tax_rate="16.00",
		total=total,
		amount_due=total,
		amount_paid=total,
	)
	order = event["data"]
	order["number"] = f"ORD-{order_id}"
	order["channel"] = "counter"
	order["sales_channel"] = {
		"id": channel["id"],
		"code": channel["code"],
		"name": channel["name"],
		"commission_amount": commission,
		"commission_tax": tax,
		"net_amount": net,
	}
	order.update(more)
	return event


def payout_event(
	payout_id, status="recorded", amount="700.00", adjustments=(), seq=50, kind="recorded", **more
):
	event = load_fixture("event_refund_created")
	payout = {
		"object": "sales_channel_payout",
		"id": payout_id,
		"channel_id": FOODPANDA["id"],
		"channel_code": "foodpanda",
		"status": status,
		"received_on": "2026-11-07",
		"period_from": "2026-11-06",
		"period_to": "2026-11-07",
		"amount_received": amount,
		"reference": f"FP-{payout_id}",
		"adjustments": [{"id": a, "label": label, "amount": value} for a, label, value in adjustments],
		"voided_at": None,
		"void_reason": None,
	}
	payout.update(more)
	event.update(
		{
			"id": f"evt-{payout_id}-{seq}",
			"seq": seq,
			"tenant_id": TENANT_A,
			"type": f"sales_channel.payout_{kind}",
			"branch_id": None,
			"entity": {"id": payout_id, "type": "sales_channel_payout"},
			"data": payout,
		}
	)
	return event


class ChannelCase(SalesTestCase):
	def setUp(self):
		super().setUp()
		# channel rows (and any account override on them) from an earlier test must not carry over
		self.clear_channel_rows()
		self.addCleanup(self.clear_channel_rows)  # they link to Customers other tests delete
		self.fake_a.lists["/sales-channels"] = [
			{**FOODPANDA, "object": "sales_channel", "active": True},
			{**CAREEM, "object": "sales_channel", "active": True},
		]
		self.fake_a.lists["/orders"] = []
		self.commission = ensure_account(COMPANY_A, "_KTA", "Channel Commission", "Direct Expenses")
		self.input_tax = ensure_account(
			COMPANY_A, "_KTA", "Input Tax on Commission", "Duties and Taxes", "Tax"
		)
		self.marketing = ensure_account(COMPANY_A, "_KTA", "Channel Adjustments", "Direct Expenses")
		self.conn_a.reload()
		self.conn_a.update(
			{
				"default_cost_center": self.acc["cost_center"],
				"channel_commission_account": self.commission,
				"channel_commission_tax_account": self.input_tax,
				"channel_payout_account": self.acc["bank"],
				"channel_adjustment_account": self.marketing,
				"partial_refunds": "Proportional Credit Note",
			}
		)
		self.conn_a.save()
		self.conn_a.reload()

	def clear_channel_rows(self):
		frappe.db.delete("Koomyo Channel Map", {"parent": ("in", [self.conn_a.name, self.conn_b.name])})
		frappe.db.commit()

	def foodpanda(self, order_id="fp-20", seq=10, **more):
		return channel_order(
			FOODPANDA,
			order_id,
			"636.84",
			"192.15",
			"30.74",
			"413.95",
			seq=seq,
			subtotal="549.00",
			tax_amount="87.84",
			**more,
		)

	def careem(self, order_id="cm-24", seq=11):
		return channel_order(CAREEM, order_id, "1490.40", "298.08", "0.00", "1192.32", seq=seq)

	def party(self, channel):
		return channels.map_row(self.conn_a, channel["id"])

	def owed_in_erpnext(self, channel):
		row = self.party(channel)
		return channels.ledger_balance(row.receivable_account, row.customer)

	def journal_entries(self, prefix=""):
		return frappe.get_all(
			"Journal Entry",
			filters={
				"koomyo_connection": self.conn_a.name,
				"koomyo_id": ("like", f"{prefix}%"),
				"docstatus": 1,
			},
			pluck="name",
		)

	def payment_entry(self, payout_id):
		name = frappe.db.get_value(
			"Payment Entry", {"koomyo_connection": self.conn_a.name, "koomyo_id": payout_id}
		)
		return frappe.get_doc("Payment Entry", name) if name else None


class TestChannelOrder(ChannelCase):
	def test_a_paid_channel_order_is_owed_by_the_partner_less_the_commission(self):
		log = self.book(self.foodpanda())
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:] if log.error else ""))
		row = self.party(FOODPANDA)
		inv = self.invoice("fp-20")
		self.assertEqual(
			(inv.customer, inv.debit_to, inv.koomyo_channel),
			(row.customer, row.receivable_account, "foodpanda"),
		)
		self.assertEqual(row.receivable_account, "Foodpanda (foodpanda) Receivable - _KTA")
		self.assertEqual(frappe.db.get_value("Account", row.receivable_account, "account_type"), "Receivable")
		self.assertEqual(D(str(inv.grand_total)).quantize(D("0.01")), D("636.84"))
		self.assertEqual(self.payments(inv), [], "no cash or bank payment: the partner pays later")
		(entry,) = self.journal_entries("commission:")
		self.assertEqual(
			gl("Journal Entry", entry),
			{self.commission: D("192.15"), self.input_tax: D("30.74"), row.receivable_account: D("-222.89")},
		)
		self.assertEqual(
			self.owed_in_erpnext(FOODPANDA), D("413.95"), "the receivable is the order's net_amount"
		)
		inv.reload()
		self.assertEqual(D(str(inv.outstanding_amount)).quantize(D("0.01")), D("413.95"))

	def test_a_replay_changes_nothing(self):
		self.book(self.foodpanda())
		again = self.foodpanda()
		again.update({"id": "evt-again", "seq": 99})
		self.assertEqual(self.book(again).status, "Done")
		self.assertEqual(len(self.journal_entries("commission:")), 1)
		self.assertEqual(len(frappe.get_all("Koomyo Channel Map", filters={"parent": self.conn_a.name})), 1)
		self.assertEqual(
			frappe.db.count(
				"Customer", {"koomyo_connection": self.conn_a.name, "koomyo_id": f"channel:{FOODPANDA['id']}"}
			),
			1,
		)
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("413.95"))

	def test_an_order_paid_before_commissions_were_recorded_counts_at_its_full_total(self):
		self.assertEqual(
			self.book(channel_order(FOODPANDA, "fp-5", "270.00", None, None, None, tax_amount="0.00")).status,
			"Done",
		)
		self.assertEqual(self.journal_entries("commission:"), [])
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("270.00"))

	def test_two_channels_have_their_own_customer_and_receivable(self):
		self.book(self.foodpanda())
		self.book(self.careem())
		a, b = self.party(FOODPANDA), self.party(CAREEM)
		self.assertNotEqual((a.customer, a.receivable_account), (b.customer, b.receivable_account))
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("413.95"))
		self.assertEqual(self.owed_in_erpnext(CAREEM), D("1192.32"))
		# a commission with no tax needs no tax account row
		(entry,) = self.journal_entries("commission:cm-24")
		self.assertEqual(
			gl("Journal Entry", entry), {self.commission: D("298.08"), b.receivable_account: D("-298.08")}
		)

	def test_a_renamed_channel_keeps_its_customer_and_account(self):
		self.book(self.foodpanda())
		before = self.party(FOODPANDA)
		renamed = self.foodpanda(order_id="fp-21", seq=12)
		renamed["data"]["sales_channel"]["name"] = "Foodpanda Pakistan"
		self.book(renamed)
		after = self.party(FOODPANDA)
		self.assertEqual(
			(after.customer, after.receivable_account), (before.customer, before.receivable_account)
		)
		self.assertEqual(after.channel_name, "Foodpanda Pakistan")
		self.assertEqual(len(frappe.get_all("Koomyo Channel Map", filters={"parent": self.conn_a.name})), 1)

	def test_the_commission_account_can_be_set_per_channel(self):
		other = ensure_account(COMPANY_A, "_KTA", "Foodpanda Commission", "Direct Expenses")
		self.book(self.foodpanda())
		frappe.db.set_value("Koomyo Channel Map", self.party(FOODPANDA).name, "commission_account", other)
		self.book(self.foodpanda(order_id="fp-21", seq=12))
		(entry,) = self.journal_entries("commission:fp-21")
		self.assertEqual(gl("Journal Entry", entry)[other], D("192.15"))

	def test_figures_that_do_not_add_up_book_nothing(self):
		event = channel_order(
			FOODPANDA, "fp-bad", "636.84", "192.15", "30.74", "413.00", subtotal="549.00", tax_amount="87.84"
		)
		log = self.book(event)
		self.assertEqual((log.status, log.error_class), ("Failed", "Fatal"))
		self.assertIsNone(self.invoice("fp-bad"))
		partial = channel_order(
			FOODPANDA,
			"fp-part",
			"636.84",
			"192.15",
			None,
			"413.95",
			seq=12,
			subtotal="549.00",
			tax_amount="87.84",
		)
		self.assertEqual(self.book(partial).error_class, "Fatal")

	def test_without_the_commission_account_the_order_waits_for_a_mapping(self):
		self.conn_a.channel_commission_account = None
		self.conn_a.save()
		log = self.book(self.foodpanda())
		self.assertEqual((log.status, log.error_class), ("Failed", "NeedsMapping"))
		self.assertIn("Channel Commission Account", log.message)
		self.assertIsNone(self.invoice("fp-20"), "nothing half booked")

	def test_an_ordinary_order_is_unchanged(self):
		event = order_event(order_id="plain-1")
		event["data"]["sales_channel"] = None
		self.assertEqual(self.book(event).status, "Done")
		inv = self.invoice("plain-1")
		self.assertFalse(inv.koomyo_channel)
		self.assertEqual(len(self.payments(inv)), 1)
		self.assertEqual(self.journal_entries("commission:"), [])


class TestChannelRefund(ChannelCase):
	def test_a_partial_refund_lowers_what_the_partner_owes_by_the_refunded_share_of_the_net(self):
		self.book(self.careem())
		log = self.book(
			refund_event(seq=30, refund_id="rf-1", order_id="cm-24", amount="100.00", method="ch_careem")
		)
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:] if log.error else ""))
		# 100.00 refunded of 1490.40, 20% commission: the partner owes 1192.32 - 80.00
		self.assertEqual(self.owed_in_erpnext(CAREEM), D("1112.32"))
		note = frappe.get_doc("Sales Invoice", log.reference_name)
		self.assertEqual((note.koomyo_channel, note.customer), ("careem", self.party(CAREEM).customer))
		self.assertEqual(self.payments(note), [], "no money moved: the receivable carries it")
		(entry,) = self.journal_entries("commission-refund:")
		self.assertEqual(
			gl("Journal Entry", entry),
			{self.party(CAREEM).receivable_account: D("20.00"), self.commission: D("-20.00")},
		)
		# a replay changes nothing
		self.book(
			refund_event(seq=31, refund_id="rf-1", order_id="cm-24", amount="100.00", method="ch_careem")
		)
		self.assertEqual(len(self.journal_entries("commission-refund:")), 1)
		self.assertEqual(self.owed_in_erpnext(CAREEM), D("1112.32"))

	def test_refunds_in_parts_add_up_to_the_whole_commission_to_the_cent(self):
		self.book(self.foodpanda())
		for n, amount in enumerate(("100.00", "33.33", "503.51")):
			log = self.book(
				refund_event(
					seq=30 + n, refund_id=f"rf-{n}", order_id="fp-20", amount=amount, method="ch_foodpanda"
				)
			)
			self.assertEqual(log.status, "Done", log.message)
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("0.00"), "all 636.84 refunded: nothing owed")

	def test_a_full_refund_of_a_channel_order_leaves_nothing_owed(self):
		self.book(self.foodpanda())
		log = self.book(
			refund_event(
				seq=30, refund_id="rf-full", order_id="fp-20", amount="636.84", method="ch_foodpanda"
			)
		)
		self.assertEqual(log.status, "Done", log.message)
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("0.00"))

	def test_a_refund_of_an_order_with_no_commission_lowers_the_receivable_by_the_refund(self):
		self.book(channel_order(FOODPANDA, "fp-5", "270.00", None, None, None))
		log = self.book(
			refund_event(seq=30, refund_id="rf-5", order_id="fp-5", amount="270.00", method="ch_foodpanda")
		)
		self.assertEqual(log.status, "Done", log.message)
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("0.00"))


class TestPayouts(ChannelCase):
	def two_orders(self):
		self.book(self.foodpanda())
		self.book(
			channel_order(
				FOODPANDA,
				"fp-7",
				"580.00",
				"175.00",
				"28.00",
				"377.00",
				seq=12,
				subtotal="500.00",
				tax_amount="80.00",
			)
		)
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("790.95"))

	def test_a_payout_settles_the_receivable_with_its_adjustments_as_deductions(self):
		self.two_orders()
		adjustments = (("adj-1", "Marketing fee", "30.00"), ("adj-2", "Late delivery penalty", "5.00"))
		log = self.book(payout_event("po-1", amount="700.00", adjustments=adjustments))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:] if log.error else ""))
		pe = self.payment_entry("po-1")
		self.assertEqual(
			(pe.docstatus, pe.reference_no, pe.party), (1, "FP-po-1", self.party(FOODPANDA).customer)
		)
		self.assertEqual(
			gl("Payment Entry", pe.name),
			{
				self.acc["bank"]: D("700.00"),
				self.marketing: D("35.00"),
				self.party(FOODPANDA).receivable_account: D("-735.00"),
			},
		)
		# owed = net_expected 790.95 - received 700.00 - adjustments 35.00
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("55.95"))
		# the same payout again (both events can repeat) changes nothing
		self.book(payout_event("po-1", amount="700.00", adjustments=adjustments, seq=51, kind="updated"))
		self.assertEqual(len(frappe.get_all("Payment Entry", filters={"koomyo_id": "po-1"})), 1)
		self.assertEqual(self.journal_entries("adjustment:"), [])
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("55.95"))

	def outstanding(self, order_id):
		return D(str(self.invoice(order_id).outstanding_amount)).quantize(D("0.01"))

	def test_a_payout_closes_the_oldest_invoices_first_and_a_void_opens_them_again(self):
		self.two_orders()  # fp-20 is owed 413.95, fp-7 is owed 377.00
		self.assertEqual((self.outstanding("fp-20"), self.outstanding("fp-7")), (D("413.95"), D("377.00")))
		self.book(payout_event("po-1", amount="500.00"))
		self.assertEqual(self.invoice("fp-20").status, "Paid")
		self.assertEqual((self.outstanding("fp-20"), self.outstanding("fp-7")), (D("0.00"), D("290.95")))
		self.assertIn(
			self.invoice("fp-7").status, ("Partly Paid", "Overdue")
		)  # Overdue once past its due date
		# the second payout settles the rest; the receivable is the sum of what is still open
		self.book(payout_event("po-2", amount="290.95", seq=51))
		self.assertEqual((self.invoice("fp-20").status, self.invoice("fp-7").status), ("Paid", "Paid"))
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("0.00"))
		# a repeat of an event changes nothing
		self.book(payout_event("po-2", amount="290.95", seq=52, kind="updated"))
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("0.00"))
		# voiding the first payout opens its invoices again (ERPNext unlinks them when it is cancelled)
		self.book(payout_event("po-1", status="voided", amount="500.00", seq=53, kind="updated"))
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("500.00"))
		self.assertEqual(self.outstanding("fp-20") + self.outstanding("fp-7"), D("500.00"))

	def test_adjustments_are_matched_with_the_payout(self):
		self.two_orders()
		self.book(payout_event("po-1", amount="755.95", adjustments=(("adj-1", "Marketing fee", "35.00"),)))
		self.assertEqual((self.invoice("fp-20").status, self.invoice("fp-7").status), ("Paid", "Paid"))
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("0.00"))

	def test_each_channel_can_have_its_own_bank_account(self):
		own = ensure_account(COMPANY_A, "_KTA", "Careem Bank", "Bank Accounts", "Bank")
		self.book(self.foodpanda())
		self.book(self.careem())
		frappe.db.set_value("Koomyo Channel Map", self.party(CAREEM).name, "payout_account", own)
		self.book(payout_event("po-fp", amount="400.00"))
		careem = payout_event(
			"po-cm", amount="1000.00", seq=51, channel_id=CAREEM["id"], channel_code="careem"
		)
		self.assertEqual(self.book(careem).status, "Done")
		self.assertEqual(
			self.payment_entry("po-fp").paid_to, self.acc["bank"], "no override: the connection's account"
		)
		self.assertEqual(self.payment_entry("po-cm").paid_to, own)
		self.assertEqual(gl("Payment Entry", self.payment_entry("po-cm").name)[own], D("1000.00"))

	def test_an_adjustment_added_later_is_one_journal_entry_and_never_twice(self):
		self.two_orders()
		self.book(payout_event("po-1", amount="700.00"))
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("90.95"))
		update = payout_event(
			"po-1",
			amount="700.00",
			adjustments=(("adj-1", "Marketing fee", "30.00"),),
			seq=51,
			kind="updated",
		)
		self.assertEqual(self.book(update).status, "Done")
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("60.95"))
		again = copy.deepcopy(update)
		again.update({"id": "evt-again", "seq": 52})
		self.book(again)
		(entry,) = self.journal_entries("adjustment:")
		self.assertEqual(
			gl("Journal Entry", entry),
			{self.marketing: D("30.00"), self.party(FOODPANDA).receivable_account: D("-30.00")},
		)
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("60.95"))

	def test_a_void_cancels_the_payment_and_its_adjustments(self):
		self.two_orders()
		self.book(payout_event("po-1", amount="700.00", adjustments=(("adj-1", "Marketing fee", "30.00"),)))
		self.book(
			payout_event(
				"po-1",
				amount="700.00",
				adjustments=(("adj-1", "Marketing fee", "30.00"), ("adj-2", "Penalty", "5.00")),
				seq=51,
				kind="updated",
			)
		)
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("55.95"))
		voided = payout_event(
			"po-1",
			status="voided",
			amount="700.00",
			adjustments=(("adj-1", "Marketing fee", "30.00"), ("adj-2", "Penalty", "5.00")),
			seq=52,
			kind="updated",
			void_reason="wrong channel",
		)
		self.assertEqual(self.book(voided).status, "Done")
		self.assertEqual(self.payment_entry("po-1").docstatus, 2)
		self.assertEqual(self.journal_entries("adjustment:"), [])
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("790.95"), "back to what the orders say")
		# a repeat of the void, and a recorded event arriving late, change nothing
		repeat = copy.deepcopy(voided)
		repeat.update({"id": "evt-repeat", "seq": 53})
		self.assertEqual(self.book(repeat).status, "Done")
		late = payout_event("po-1", amount="700.00", seq=54, kind="recorded")
		self.assertEqual(self.book(late).status, "Skipped")
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("790.95"))

	def test_a_voided_payout_that_was_never_booked_is_skipped_and_an_unseen_recorded_one_is_made_from_its_data(
		self,
	):
		self.two_orders()
		self.assertEqual(
			self.book(payout_event("po-void", status="voided", amount="500.00", kind="updated")).status,
			"Skipped",
		)
		self.assertIsNone(self.payment_entry("po-void"))
		self.assertEqual(
			self.book(payout_event("po-late", amount="100.00", kind="updated", seq=51)).status, "Done"
		)
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("690.95"))

	def test_without_the_payout_bank_account_it_waits_and_then_goes_through_on_retry(self):
		self.two_orders()
		self.conn_a.channel_payout_account = None
		self.conn_a.save()
		log = self.book(payout_event("po-1", amount="700.00"))
		self.assertEqual((log.status, log.error_class), ("Failed", "NeedsMapping"))
		self.assertIn("Payout Bank Account", log.message)
		self.assertIsNone(self.payment_entry("po-1"))
		self.conn_a.channel_payout_account = self.acc["bank"]
		self.conn_a.save()
		from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry
		from koomyo_erpnext.sync import events

		retry([log.name])
		with self.koomyo():
			events.process_pending(self.conn_a.name)
		self.assertEqual(frappe.db.get_value("Koomyo Event Log", log.name, "status"), "Done")
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("90.95"))


class NightlyBase(ChannelCase):
	def setUp(self):
		super().setUp()
		self.two = lambda: (self.book(self.foodpanda()), self.book(self.careem()))

	def expect(self, foodpanda_owed, careem_owed="1192.32"):
		self.fake_a.channel_owed = {FOODPANDA["id"]: foodpanda_owed, CAREEM["id"]: careem_owed}

	def check(self, **kw):
		with self.koomyo():
			return channels.check_connection(self.conn_a.name, **kw)

	def attention(self):
		return frappe.get_all(
			"Koomyo Event Log",
			filters={"connection": self.conn_a.name, "event_type": channels.CHECK_TYPE},
			fields=["name", "status", "error_class", "message"],
		)


class TestNightlyCheck(NightlyBase):
	def test_when_the_receivable_matches_every_channel_nothing_happens(self):
		self.two()
		self.expect("413.95")
		summary = self.check()
		self.assertEqual(
			{c["channel"]: c["status"] for c in summary["channels"]}, {"foodpanda": "ok", "careem": "ok"}
		)
		self.assertEqual(self.attention(), [])

	def test_a_missed_payout_is_caught_up_from_the_channels_payout_list(self):
		self.two()
		# Koomyo has a payout of 400.00 that no event brought: it owes 13.95
		self.expect("13.95")
		self.fake_a.payouts[FOODPANDA["id"]] = [payout_event("po-missed", amount="400.00")["data"]]
		dry = self.check(dry_run=True)
		self.assertEqual(dry["channels"][0]["status"], "mismatch")
		self.assertIsNone(self.payment_entry("po-missed"), "a dry run writes nothing")
		summary = self.check()
		self.assertEqual(summary["channels"][0]["status"], "caught up")
		self.assertEqual(summary["caught_up"], 1)
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("13.95"))
		self.assertEqual(self.attention(), [])

	def test_a_missed_order_is_booked_from_the_channels_order_list(self):
		self.book(self.foodpanda())
		missed = self.careem()["data"]
		self.fake_a.lists["/orders"] = [missed]
		self.expect("413.95")
		summary = self.check()
		self.assertEqual({c["channel"]: c["status"] for c in summary["channels"]}["careem"], "caught up")
		self.assertEqual(self.owed_in_erpnext(CAREEM), D("1192.32"))

	def test_a_difference_that_cannot_be_fixed_is_raised_once_a_day_with_both_numbers(self):
		self.two()
		self.expect("999.00")
		self.assertEqual(self.check()["channels"][0]["status"], "mismatch")
		self.check()
		(entry,) = self.attention()
		self.assertEqual((entry.status, entry.error_class), ("Failed", "Fatal"))
		for text in ("999.00", "413.95", "585.05"):
			self.assertIn(text, entry.message)


class TestTwoConnections(ChannelCase):
	def test_the_same_channel_id_on_two_connections_stays_in_its_own_company(self):
		acc_b = configure_sales(self.conn_b, "_KTB")
		self.conn_b.reload()
		self.conn_b.update(
			{
				"default_cost_center": acc_b["cost_center"],
				"channel_commission_account": ensure_account(
					COMPANY_B, "_KTB", "Channel Commission", "Direct Expenses"
				),
				"channel_commission_tax_account": ensure_account(
					COMPANY_B, "_KTB", "Input Tax on Commission", "Duties and Taxes", "Tax"
				),
			}
		)
		self.conn_b.save()
		frappe.db.delete("Koomyo Channel Map", {"parent": self.conn_b.name})
		self.fake_b.lists["/sales-channels"] = []
		self.book(self.foodpanda())
		other = self.foodpanda(seq=10)
		other["tenant_id"] = TENANT_B
		other["id"] = "evt-b"
		log = self.book(other, connection=self.conn_b, fake=self.fake_b)
		self.assertEqual(log.status, "Done", log.message)
		a = self.party(FOODPANDA)
		b = channels.map_row(self.conn_b, FOODPANDA["id"])
		self.assertNotEqual(a.customer, b.customer)
		self.assertTrue(a.receivable_account.endswith("_KTA") and b.receivable_account.endswith("_KTB"))
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", self.invoice("fp-20").name, "company"), COMPANY_A
		)
		self.assertEqual(self.invoice("fp-20", self.conn_b).company, COMPANY_B)
		self.assertEqual(self.owed_in_erpnext(FOODPANDA), D("413.95"))
		self.assertEqual(channels.ledger_balance(b.receivable_account, b.customer), D("413.95"))


class TestNightlyRefunds(NightlyBase):
	def test_a_missed_refund_is_caught_up_and_an_old_difference_is_closed(self):
		self.two()
		# Koomyo: Careem order cm-24 had 100.00 refunded (owes 1192.32 - 80.00); no refund event arrived
		order = self.careem()["data"]
		order["amounts"]["amount_refunded"] = "100.00"
		self.fake_a.lists["/orders"] = [order]
		refund = refund_event(refund_id="rf-missed", order_id="cm-24", amount="100.00", method="ch_careem")[
			"data"
		]
		self.fake_a.lists["/refunds"] = [refund]
		self.expect("413.95", "1112.32")
		summary = self.check()
		self.assertEqual({c["channel"]: c["status"] for c in summary["channels"]}["careem"], "caught up")
		self.assertEqual(self.owed_in_erpnext(CAREEM), D("1112.32"))
		self.assertEqual(self.check()["caught_up"], 0, "a second night books nothing")

	def test_a_difference_flagged_earlier_is_closed_when_the_sides_agree(self):
		self.two()
		self.expect("999.00")
		self.check()
		(entry,) = self.attention()
		self.assertEqual(entry.status, "Failed")
		self.expect("413.95")
		self.check()
		self.assertEqual(frappe.db.get_value("Koomyo Event Log", entry.name, "status"), "Done")
