"""Slice 6: Koomyo Backfill - past ranges from the list endpoints through the live handlers.

CLAUDE.md asks for: dry run counts, same range twice creates nothing, closed period refused,
rate-limit pacing, resume after stop. Records are the docs' order example with other ids.
"""

import copy
from unittest.mock import patch

import frappe

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_backfill.koomyo_backfill import create_for_all_connections
from koomyo_erpnext.sync import backfill as engine
from koomyo_erpnext.sync import events
from koomyo_erpnext.tests.fake_koomyo import error_response, load_fixture
from koomyo_erpnext.tests.helpers import COMPANY_A, TENANT_A, configure_stock
from koomyo_erpnext.tests.test_sales import SalesTestCase
from koomyo_erpnext.tests.test_stock import KITCHEN, adjustment

DAY = "2026-10-12"
BRANCH = "90000000-0000-4000-8000-000000000001"


def order(order_id, day=DAY, amount="724.50", method="cash"):
	data = copy.deepcopy(load_fixture("event_order_paid")["data"])
	data.update(
		{"id": order_id, "number": f"ORD-{order_id}", "business_date": day, "updated_at": f"{day}T16:53:19Z"}
	)
	data["payment"]["payments"][0].update({"id": f"pay-{order_id}", "method": method})
	return data


class BackfillTestCase(SalesTestCase):
	def setUp(self):
		super().setUp()
		self.sleeps = []
		patcher = patch.object(engine, "SLEEP", self.sleeps.append)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.fake_a.lists["/orders"] = [order("o1"), order("o2"), order("o3", day="2026-10-13")]
		self.fake_a.lists["/refunds"] = []

	def tearDown(self):
		for name in frappe.get_all(
			"Accounting Period", filters={"name": ("like", "_Koomyo closed%")}, pluck="name"
		):
			frappe.db.delete("Closed Document", {"parent": name})
			frappe.db.delete("Accounting Period", {"name": name})
		frappe.db.commit()
		super().tearDown()

	def make(self, **values):
		doc = frappe.get_doc(
			{
				"doctype": "Koomyo Backfill",
				"connection": self.conn_a.name,
				"from_date": DAY,
				"to_date": "2026-10-13",
				**values,
			}
		)
		doc.insert()
		frappe.db.commit()
		return doc

	def press(self, doc, action="start", **kwargs):
		"""Press the button, then run the queued job here (frappe.enqueue is captured in tests)."""
		doc.reload()
		getattr(doc, action)(**kwargs)
		frappe.db.commit()
		job = [kw for _a, kw in self.enqueued if kw.get("backfill_name") == doc.name][-1]
		with self.koomyo():
			engine.run(
				doc.name,
				dry_run=job["dry_run"],
				only_chunk=job["only_chunk"],
				retry_failed=job["retry_failed"],
			)
		doc.reload()
		return doc

	def invoices(self):
		return frappe.get_all(
			"Sales Invoice",
			filters={"koomyo_connection": self.conn_a.name, "is_return": 0},
			fields=["koomyo_id", "posting_date"],
		)


class TestBackfill(BackfillTestCase):
	def test_preview_counts_and_writes_nothing(self):
		# o1 already came in live.
		live = load_fixture("event_order_paid") | {"tenant_id": TENANT_A}
		live["data"] = order("o1")
		self.assertEqual(self.book(live).status, "Done")
		logs_before = frappe.db.count("Koomyo Event Log")
		items_before = frappe.db.count("Item")

		doc = self.press(self.make(), "preview")
		self.assertEqual(doc.status, "Previewed", doc.error)
		self.assertEqual(
			(doc.found, doc.already_there, doc.created, doc.skipped, doc.failed), (3, 1, 2, 0, 0)
		)
		self.assertEqual([s["id"] for s in frappe.parse_json(doc.preview_sample)], ["o2", "o3"])
		self.assertEqual(len(self.invoices()), 1, "a preview writes nothing")
		self.assertEqual(frappe.db.count("Koomyo Event Log"), logs_before)
		self.assertEqual(frappe.db.count("Item"), items_before)

	def test_run_posts_with_koomyo_dates_and_twice_changes_nothing(self):
		doc = self.press(self.make())
		self.assertEqual(doc.status, "Completed", doc.error)
		self.assertEqual((doc.found, doc.created, doc.already_there, doc.progress), (3, 3, 0, 100))
		self.assertEqual(
			sorted((i.koomyo_id, str(i.posting_date)) for i in self.invoices()),
			[("o1", DAY), ("o2", DAY), ("o3", "2026-10-13")],
		)
		logs = frappe.get_all(
			"Koomyo Event Log", filters={"backfill": doc.name}, fields=["received_via", "status"]
		)
		self.assertEqual({(l.received_via, l.status) for l in logs}, {("Backfill", "Done")})

		again = self.press(self.make())
		self.assertEqual((again.found, again.created, again.already_there), (3, 0, 3))
		self.assertEqual(len(self.invoices()), 3)
		# And what came live later is not doubled either.
		live = load_fixture("event_order_paid") | {"tenant_id": TENANT_A, "seq": 50}
		live["data"] = order("o2")
		self.assertEqual(self.book(live).status, "Done")
		self.assertEqual(len(self.invoices()), 3)

	def test_paging_with_opaque_cursors(self):
		self.fake_a.lists["/orders"] = [order(f"p{i}") for i in range(5)]
		self.fake_a.page_cap = 2
		doc = self.press(self.make(to_date=DAY))
		self.assertEqual((doc.found, doc.created), (5, 5))
		cursors = [c["params"].get("cursor") for c in self.fake_a.calls if c["path"] == "/orders"]
		self.assertEqual(cursors[0], None)
		self.assertTrue(all(cursors[1:]), "later pages pass back Koomyo's cursor as given")

	def test_closed_period_is_refused_by_name(self):
		frappe.get_doc(
			{
				"doctype": "Accounting Period",
				"period_name": "_Koomyo closed",
				"company": COMPANY_A,
				"start_date": "2026-10-01",
				"end_date": "2026-10-20",
				"closed_documents": [{"document_type": "Sales Invoice", "closed": 1}],
			}
		).insert()
		doc = self.make()
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.preview()
		self.assertIn("_Koomyo closed", str(ctx.exception))
		frappe.db.rollback()

	def test_frozen_stock_is_refused(self):
		configure_stock(self.conn_a, "_KTA")
		frappe.db.set_single_value("Stock Settings", "stock_frozen_upto", "2026-10-12")
		try:
			doc = self.make(bring_sales=0, bring_stock=1)
			with self.assertRaises(frappe.ValidationError) as ctx:
				doc.start()
			self.assertIn("Stock is frozen", str(ctx.exception))
		finally:
			frappe.db.set_single_value("Stock Settings", "stock_frozen_upto", None)
			frappe.db.commit()

	def test_rate_limit_pacing_and_long_retry_after(self):
		self.fake_a.rate_limit = (60, 1)  # nearly empty allowance on every answer
		self.fake_a.scripted["/orders"] = [
			error_response(429, "rate_limited", headers={"Retry-After": "120"})
		]
		doc = self.press(self.make(to_date=DAY))
		self.assertEqual((doc.status, doc.created), ("Completed", 2))
		self.assertIn(121, self.sleeps, "a long Retry-After is waited out, not failed")
		self.assertTrue([s for s in self.sleeps if s != 121], "calls slow down when the allowance runs low")

	def test_stop_then_resume_from_saved_position(self):
		self.fake_a.lists["/orders"] = [order(f"r{i}") for i in range(4)]
		self.fake_a.page_cap = 2
		doc = self.make(to_date=DAY)
		real = events.process_event
		calls = []

		def stop_after_two(name):
			real(name)
			calls.append(name)
			if len(calls) == 2:
				frappe.db.set_value("Koomyo Backfill", doc.name, "status", "Stopping")
				frappe.db.commit()

		with patch.object(events, "process_event", stop_after_two):
			doc = self.press(doc)
		self.assertEqual(doc.status, "Stopped")
		self.assertEqual(len(self.invoices()), 2)
		state = frappe.parse_json(doc.chunks[0].state)
		self.assertTrue(state["cursors"]["order:/orders:"], "the page position is saved")

		self.fake_a.calls.clear()
		doc = self.press(doc)
		self.assertEqual(doc.status, "Completed", doc.error)
		self.assertEqual(len(self.invoices()), 4)
		first = next(c for c in self.fake_a.calls if c["path"] == "/orders")
		self.assertEqual(
			first["params"].get("cursor"), state["cursors"]["order:/orders:"], "resumes from the saved cursor"
		)

	def test_missing_mapping_skipped_reported_then_retry_failed(self):
		self.fake_a.lists["/orders"] = [order("m1", method="jazzcash")]
		doc = self.press(self.make(to_date=DAY))
		self.assertEqual((doc.found, doc.skipped, doc.created), (1, 1, 0))
		self.assertIn("jazzcash", doc.chunks[0].message)

		self.conn_a.reload()
		self.conn_a.append(
			"payment_map",
			{"koomyo_method": "jazzcash", "mode_of_payment": "Wire Transfer", "account": self.acc["bank"]},
		)
		self.conn_a.save()
		doc = self.press(doc, "rerun_day", chunk=DAY)
		self.assertEqual((doc.found, doc.created), (1, 1))

	def test_retry_failed(self):
		self.fake_a.lists["/orders"] = [order("f1")]
		bad = order("f1")
		bad["amounts"]["amount_due"] = "1.00"  # does not add up: Fatal
		self.fake_a.lists["/orders"] = [bad]
		doc = self.press(self.make(to_date=DAY))
		self.assertEqual(doc.failed, 1)
		log = frappe.get_doc("Koomyo Event Log", {"backfill": doc.name})
		fixed = order("f1")
		log.db_set("payload", frappe.as_json(engine.envelope(self.conn_a, "order", fixed)))
		frappe.db.commit()
		doc = self.press(doc, "retry_failed")
		self.assertEqual((doc.failed, doc.created), (0, 1))

	def test_inputs_are_checked(self):
		for values, text in (
			({"to_date": "2099-01-01"}, "future"),
			({"bring_sales": 0}, "at least one"),
			({"bring_shifts": 1}, "Sync Shifts"),
			({"koomyo_branch_id": "unknown"}, "Branch Map"),
		):
			with self.assertRaises(frappe.ValidationError, msg=text) as ctx:
				self.make(**values)
			self.assertIn(text, str(ctx.exception))

	def test_stock_needs_the_opening_stock_date(self):
		configure_stock(self.conn_a, "_KTA", stock_start_date="2026-10-13")
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.make(bring_sales=0, bring_stock=1)
		self.assertIn("Opening Stock Date", str(ctx.exception))

	def test_stock_usage_rebuilt_without_doubling_live(self):
		configure_stock(self.conn_a, "_KTA")
		self.conn_a.reload()
		self.fake_a.add_ingredient(KITCHEN, "Kitchen stock", "pcs", "12.5000")
		self.assertEqual(self.book(adjustment(1, "20.0000")).status, "Done")

		def move(mid, qty, reason, day):
			return {
				"id": mid,
				"object": "stock_movement",
				"ingredient_id": KITCHEN,
				"ingredient": "Kitchen stock",
				"unit": "pcs",
				"quantity_change": qty,
				"unit_cost": "12.5000",
				"value": "0",
				"reason": reason,
				"reference_id": None,
				"location": {"type": "branch", "id": BRANCH},
				"created_at": f"{day}T12:00:00Z",
				"business_date": day,
			}

		self.fake_a.lists["/stock-movements"] = [
			move("m1", "-5.0000", "order_deduction", "2026-10-11"),
			move("m2", "1.0000", "order_cancel_restore", "2026-10-11"),
			move("m3", "-3.0000", "order_deduction", DAY),
			move("m4", "-9.0000", "spilled", DAY),  # a manual change: never counted as usage
		]
		for path in ("/waste", "/stock-counts", "/stock-transfers"):
			self.fake_a.lists[path] = []
		# The live usage event for 2026-10-11 already came in.
		live = load_fixture("event_stock_usage_daily") | {"tenant_id": TENANT_A, "seq": 2}
		live["data"]["lines"][0]["quantity_used"] = "4.0000"
		self.assertEqual(self.book(live).status, "Done")

		doc = self.press(self.make(from_date="2026-10-11", to_date=DAY, bring_sales=0, bring_stock=1))
		self.assertEqual(doc.status, "Completed", doc.error)
		self.assertEqual((doc.found, doc.already_there, doc.created), (2, 1, 1))
		entry = frappe.db.get_value(
			"Stock Entry", {"koomyo_connection": self.conn_a.name, "koomyo_id": f"usage:{BRANCH}:{DAY}"}
		)
		self.assertEqual(frappe.db.get_value("Stock Entry Detail", {"parent": entry}, "qty"), 3)

	def test_all_connections_makes_one_per_connection(self):
		from koomyo_erpnext.tests.helpers import configure_sales

		configure_sales(self.conn_b, "_KTB")
		result = create_for_all_connections(DAY, DAY, bring_sales=1)
		made_for = frappe.get_all(
			"Koomyo Backfill", filters={"name": ("in", result["made"])}, pluck="connection"
		)
		self.assertIn(self.conn_a.name, made_for)
		self.assertIn(self.conn_b.name, made_for)
		self.assertEqual(len(made_for), len(set(made_for)), "one backfill per connection")

	def test_user_sees_only_own_company_backfills(self):
		from koomyo_erpnext.tests.helpers import configure_sales
		from koomyo_erpnext.tests.test_permissions import USER

		configure_sales(self.conn_b, "_KTB")
		create_for_all_connections(DAY, DAY, bring_sales=1)
		if not frappe.db.exists("User", USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": USER,
					"first_name": "Accountant A",
					"send_welcome_email": 0,
					"roles": [{"role": "Koomyo Manager"}],
				}
			).insert(ignore_permissions=True)
		frappe.db.delete("User Permission", {"user": USER})
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": USER,
				"allow": "Company",
				"for_value": COMPANY_A,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		frappe.set_user(USER)
		try:
			self.assertEqual(frappe.get_list("Koomyo Backfill", pluck="company"), [COMPANY_A])
		finally:
			frappe.set_user("Administrator")
			frappe.db.delete("User Permission", {"user": USER})
			frappe.db.commit()


class TestBackfillFailure(BackfillTestCase):
	def test_koomyo_down_fails_plainly_and_resumes(self):
		self.fake_a.down = True
		with patch.object(KoomyoClient, "_backoff", staticmethod(lambda attempt: 0)):
			doc = self.press(self.make(to_date=DAY))
		self.assertEqual(doc.status, "Failed")
		self.assertIn("Could not reach Koomyo", doc.error)
		self.assertEqual(doc.chunks[0].status, "Failed", "the day it was on is not left as Running")
		self.fake_a.down = False
		doc = self.press(doc)
		self.assertEqual((doc.status, doc.created), ("Completed", 2))
