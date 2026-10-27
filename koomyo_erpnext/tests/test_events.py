from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import NeedsMapping, Retryable, Skipped
from koomyo_erpnext.sync import events
from koomyo_erpnext.tests.fake_koomyo import error_response
from koomyo_erpnext.tests.helpers import (
	KEY_A,
	SECRET_A,
	KoomyoIntegrationTestCase,
	event_body,
	post_webhook,
	sign,
	use_test_handler,
)


class EventsTestCase(KoomyoIntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.handled = []
		self.behaviour = {}
		use_test_handler(self, "shift.opened", self.handler, scopes=("shifts.read",))

	def handler(self, event, connection):
		"""A test handler: makes a ToDo so we can see documents roll back on failure."""
		todo = frappe.get_doc({"doctype": "ToDo", "description": f"koomyo {event['id']}"}).insert(
			ignore_permissions=True
		)
		self.handled.append((connection.name, event["seq"]))
		action = self.behaviour.get(event["id"])
		if action:
			raise action
		return ("ToDo", todo.name)

	def add_shift(self, fake, seq, event_id=None):
		return fake.add_event(
			"event_shift_opened", seq=seq, id=event_id or f"evt-{seq}", created="2026-10-12T16:52:51Z"
		)

	def todos(self, event_id):
		return frappe.get_all("ToDo", filters={"description": f"koomyo {event_id}"}, pluck="name")

	def tearDown(self):
		frappe.db.delete("ToDo", {"description": ("like", "koomyo %")})
		super().tearDown()


class TestProcessing(EventsTestCase):
	def test_feed_order_and_reference(self):
		for seq in (7, 3, 5):
			self.add_shift(self.fake_a, seq)
		with self.koomyo():
			events.catch_up(self.conn_a.name)
		events.process_pending(self.conn_a.name)
		self.assertEqual(self.handled, [("Test A", 3), ("Test A", 5), ("Test A", 7)])
		for row in self.logs(self.conn_a.name):
			self.assertEqual(row.status, "Done")
			self.assertEqual(row.reference_doctype, "ToDo")
			self.assertEqual(row.attempts, 1)

	def test_needs_mapping_rolls_back_and_replays_after_fix(self):
		self.add_shift(self.fake_a, 1, "e1")
		self.behaviour["e1"] = NeedsMapping("Add a Koomyo Branch Map row for branch X.")
		with self.koomyo():
			events.catch_up(self.conn_a.name)
		events.process_pending(self.conn_a.name)
		row = self.logs(self.conn_a.name)[0]
		self.assertEqual((row.status, row.error_class), ("Failed", "NeedsMapping"))
		self.assertIn("Branch Map", row.message)
		self.assertEqual(self.todos("e1"), [], "a failed handler must leave nothing behind")

		# Not retried on its own.
		events.process_pending(self.conn_a.name)
		self.assertEqual(len(self.handled), 1)

		# The map row is added; Retry replays it.
		del self.behaviour["e1"]
		from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log import retry

		retry([row.name])
		events.process_pending(self.conn_a.name)
		row = self.logs(self.conn_a.name)[0]
		self.assertEqual(row.status, "Done")
		self.assertEqual(len(self.todos("e1")), 1)

	def test_retryable_backs_off_then_retries(self):
		self.add_shift(self.fake_a, 1, "e1")
		self.behaviour["e1"] = Retryable("Invoice not booked yet.")
		with self.koomyo():
			events.catch_up(self.conn_a.name)
		events.process_pending(self.conn_a.name)
		row = self.logs(self.conn_a.name)[0]
		self.assertEqual((row.status, row.error_class), ("Failed", "Retryable"))
		self.assertGreater(row.next_retry_on, now_datetime())
		events.process_pending(self.conn_a.name)
		self.assertEqual(len(self.handled), 1, "not before the wait is over")

		del self.behaviour["e1"]
		frappe.db.set_value(
			"Koomyo Event Log", row.name, "next_retry_on", add_to_date(now_datetime(), minutes=-1)
		)
		events.process_pending(self.conn_a.name)
		row = self.logs(self.conn_a.name)[0]
		self.assertEqual((row.status, row.attempts), ("Done", 2))

	def test_retryable_gives_up_after_max(self):
		frappe.db.set_single_value("Koomyo Settings", "max_auto_retries", 1)
		self.add_shift(self.fake_a, 1, "e1")
		self.behaviour["e1"] = Retryable("still down")
		with self.koomyo():
			events.catch_up(self.conn_a.name)
		for _ in range(3):
			frappe.db.set_value(
				"Koomyo Event Log",
				{"event_id": "e1"},
				"next_retry_on",
				add_to_date(now_datetime(), minutes=-1),
			)
			events.process_pending(self.conn_a.name)
		row = self.logs(self.conn_a.name)[0]
		# Third pass: the row is past the limit, so it is not tried again.
		self.assertEqual(row.attempts, 2)
		self.assertEqual(len(self.handled), 2)
		self.assertIn("Gave up", row.message)

	def test_skipped_and_unexpected_errors(self):
		self.add_shift(self.fake_a, 1, "e1")
		self.add_shift(self.fake_a, 2, "e2")
		self.behaviour["e1"] = Skipped("Order is not paid.")
		self.behaviour["e2"] = ZeroDivisionError(f"boom {KEY_A}")
		with self.koomyo():
			events.catch_up(self.conn_a.name)
		events.process_pending(self.conn_a.name)
		e1, e2 = self.logs(self.conn_a.name)
		self.assertEqual(e1.status, "Skipped")
		self.assertEqual((e2.status, e2.error_class), ("Failed", "Fatal"))
		self.assertIn("ZeroDivisionError", e2.error)
		self.assertNotIn(KEY_A, e2.error)
		self.assertEqual(self.todos("e1") + self.todos("e2"), [])

	def test_stuck_processing_row_is_picked_up(self):
		self.add_shift(self.fake_a, 1, "e1")
		with self.koomyo():
			events.catch_up(self.conn_a.name)
		name = self.logs(self.conn_a.name)[0].name
		frappe.db.set_value("Koomyo Event Log", name, "status", "Processing", update_modified=False)
		frappe.db.set_value(
			"Koomyo Event Log", name, "modified", add_to_date(now_datetime(), hours=-2), update_modified=False
		)
		events.process_pending(self.conn_a.name)
		self.assertEqual(self.logs(self.conn_a.name)[0].status, "Done")

	def test_disabled_connection_not_processed(self):
		self.add_shift(self.fake_a, 1)
		with self.koomyo():
			events.catch_up(self.conn_a.name)
		self.conn_a.db_set("enabled", 0)
		events.process_pending(self.conn_a.name)
		self.assertEqual(self.handled, [])


class TestCatchUp(EventsTestCase):
	def test_reads_pages_and_saves_position(self):
		for seq in range(1, 8):
			self.add_shift(self.fake_a, seq)
		with self.koomyo():
			original = events.FEED_PAGE_SIZE
			events.FEED_PAGE_SIZE = 3
			try:
				summary = events.catch_up(self.conn_a.name)
			finally:
				events.FEED_PAGE_SIZE = original
		self.assertEqual(summary["stored"], 7)
		self.assertEqual(summary["pages"], 3)
		self.assertTrue(summary["done"])
		conn = frappe.get_doc("Koomyo Connection", self.conn_a.name)
		self.assertEqual(conn.last_seq, 7)
		self.assertIsNotNone(conn.last_caught_up_on)
		calls = [c for c in self.fake_a.calls if c["path"] == "/events"]
		self.assertEqual([c["params"].get("after") for c in calls], [0, 3, 6])
		self.assertEqual(calls[0]["params"]["since"], "2026-10-01")
		self.assertNotIn("since", calls[1]["params"])
		self.assertIn("shift.opened", calls[0]["params"]["types"].split(","))

	def test_feed_and_webhook_share_the_duplicate_guard(self):
		event = self.add_shift(self.fake_a, 1, "e1")
		body = event_body(event)
		self.assertEqual(post_webhook(self.conn_a.webhook_token, body, sign(SECRET_A, body))[0], 200)
		with self.koomyo():
			summary = events.catch_up(self.conn_a.name)
		self.assertEqual((summary["stored"], summary["duplicates"]), (0, 1))
		events.process_pending(self.conn_a.name)
		events.process_pending(self.conn_a.name)
		self.assertEqual(len(self.handled), 1)
		self.assertEqual(len(self.todos("e1")), 1)

	def test_downtime_catch_up_nothing_missed_or_doubled(self):
		# Two events came by webhook before the outage, three happened while we were down.
		for seq in (1, 2):
			event = self.add_shift(self.fake_a, seq)
			body = event_body(event)
			post_webhook(self.conn_a.webhook_token, body, sign(SECRET_A, body))
		events.process_pending(self.conn_a.name)
		for seq in (3, 4, 5):
			self.add_shift(self.fake_a, seq)
		with self.koomyo():
			events.catch_up(self.conn_a.name)
			events.catch_up(self.conn_a.name)
		events.process_pending(self.conn_a.name)
		self.assertEqual(sorted(s for _c, s in self.handled), [1, 2, 3, 4, 5])
		self.assertEqual(len(self.logs(self.conn_a.name, status="Done")), 5)

	def test_second_run_continues_from_saved_position(self):
		self.add_shift(self.fake_a, 1)
		with self.koomyo():
			events.catch_up(self.conn_a.name)
			self.add_shift(self.fake_a, 2)
			summary = events.catch_up(self.conn_a.name)
		self.assertEqual(summary["stored"], 1)
		self.assertEqual(self.fake_a.calls[-1]["params"]["after"], 1)

	def test_position_not_saved_when_page_fails(self):
		self.add_shift(self.fake_a, 1)
		self.fake_a.scripted["/events"] = [error_response(500, "internal_error") for _ in range(4)]
		with self.koomyo():
			with patch.object(KoomyoClient, "_backoff", staticmethod(lambda attempt: 0)):
				summary = events.catch_up(self.conn_a.name)
		self.assertIn("error", summary)
		conn = frappe.get_doc("Koomyo Connection", self.conn_a.name)
		self.assertEqual(conn.last_seq, 0)
		self.assertEqual(conn.status, "Connected", "a passing outage does not need a person")

	def test_invalid_event_in_feed_stops_and_flags(self):
		self.add_shift(self.fake_a, 1)
		self.fake_a.events[0]["tenant_id"] = "someone-else"
		with self.koomyo():
			events.catch_up(self.conn_a.name)
		conn = frappe.get_doc("Koomyo Connection", self.conn_a.name)
		self.assertEqual(conn.status, "Needs Attention")
		self.assertEqual(conn.last_seq, 0)
		self.assertEqual(self.logs(), [])

	def test_revoked_key_needs_attention_and_alerts_once_a_day(self):
		frappe.db.set_single_value("Koomyo Settings", "alert_email", "alerts@example.test")
		frappe.db.commit()
		self.fake_a.scripted["/events"] = [
			error_response(401, "invalid_api_key"),
			error_response(401, "invalid_api_key"),
		]
		# sendmail is captured: in tests it would go through the patched frappe.enqueue.
		with self.koomyo(), patch("frappe.sendmail") as sendmail:
			events.catch_up(self.conn_a.name)
			events.catch_up(self.conn_a.name)
			events.daily_alerts()
		conn = frappe.get_doc("Koomyo Connection", self.conn_a.name)
		self.assertEqual(conn.status, "Needs Attention")
		self.assertEqual(sendmail.call_count, 1)
		self.assertEqual(sendmail.call_args.kwargs["recipients"], ["alerts@example.test"])
		self.assertNotIn(KEY_A, str(sendmail.call_args))

	def test_recovers_from_needs_attention(self):
		self.conn_a.db_set({"status": "Needs Attention", "status_message": "x"})
		with self.koomyo():
			events.catch_up(self.conn_a.name)
		self.assertEqual(frappe.db.get_value("Koomyo Connection", self.conn_a.name, "status"), "Connected")

	def test_a_down_does_not_delay_b(self):
		self.add_shift(self.fake_a, 1)
		self.add_shift(self.fake_b, 1)
		self.fake_a.down = True
		with self.koomyo():
			with patch.object(KoomyoClient, "_backoff", staticmethod(lambda attempt: 0)):
				summary_a = events.catch_up(self.conn_a.name)
				summary_b = events.catch_up(self.conn_b.name)
		self.assertIn("error", summary_a)
		self.assertEqual(summary_b["stored"], 1)
		events.process_pending(self.conn_b.name)
		self.assertEqual(self.handled, [("Test B", 1)])

	def test_scheduler_gives_each_connection_its_own_jobs(self):
		events.schedule_catch_up()
		mine = (self.conn_a.name, self.conn_b.name)
		jobs = [
			kw for _a, kw in self.enqueued if _a[0].endswith(".catch_up") and kw["connection_name"] in mine
		]
		self.assertEqual(sorted(kw["connection_name"] for kw in jobs), sorted(mine))
		self.assertEqual(len({kw["job_id"] for kw in jobs}), 2)
		self.assertTrue(all(kw["deduplicate"] for _a, kw in self.enqueued))

	def test_nothing_handled_reads_nothing(self):
		with self.koomyo(), patch.dict(events.DISPATCH, clear=True):
			summary = events.catch_up(self.conn_a.name)
		self.assertTrue(summary["done"])
		self.assertEqual(self.fake_a.calls, [])


class TestCleanup(EventsTestCase):
	def test_old_done_rows_deleted_failed_kept(self):
		for seq in (1, 2, 3):
			self.add_shift(self.fake_a, seq, f"e{seq}")
		self.behaviour["e3"] = NeedsMapping("map it")
		with self.koomyo():
			events.catch_up(self.conn_a.name)
		events.process_pending(self.conn_a.name)
		old = add_to_date(now_datetime(), days=-40)
		frappe.db.sql("update `tabKoomyo Event Log` set modified=%s where event_id in ('e1', 'e3')", old)
		events.cleanup_event_log()
		self.assertEqual(sorted(r.event_id for r in self.logs()), ["e2", "e3"])


class TestConcurrency(EventsTestCase):
	def test_deadlock_is_retried_not_left_processing(self):
		self.add_shift(self.fake_a, 1, "e1")

		def deadlock(event, connection):
			frappe.db.rollback()  # what the database does on a deadlock: the savepoint is gone too
			raise frappe.QueryDeadlockError("Deadlock found when trying to get lock")

		use_test_handler(self, "shift.opened", deadlock)
		with self.koomyo():
			events.catch_up(self.conn_a.name)
		events.process_pending(self.conn_a.name)
		row = self.logs(self.conn_a.name)[0]
		self.assertEqual((row.status, row.error_class), ("Failed", "Retryable"))
		self.assertIn("busy", row.message)
		self.assertIsNotNone(row.next_retry_on)

	def test_only_one_processor_per_connection(self):
		self.add_shift(self.fake_a, 1, "e1")
		with self.koomyo():
			events.catch_up(self.conn_a.name)
		with patch.object(events, "acquire_lock", return_value=False):
			events.process_pending(self.conn_a.name)
		self.assertEqual(self.handled, [], "another processor holds the connection")
		events.process_pending(self.conn_a.name)
		self.assertEqual(len(self.handled), 1)

	def test_lock_is_released_after_processing(self):
		events.process_pending(self.conn_a.name)
		lock = events.processing_lock_name(self.conn_a.name)
		self.assertEqual(frappe.db.sql("select is_free_lock(%s)", (lock,))[0][0], 1)
