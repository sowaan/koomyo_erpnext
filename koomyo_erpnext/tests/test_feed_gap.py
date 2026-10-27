"""Section 3: the feed says where it starts (`oldest_seq`, `latest_seq`, `gap`).

The 29-day guess is gone: only `gap` stops the feed. On a gap the app reads the lists again (a
backfill) and continues from `latest_seq`.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import frappe

from koomyo_erpnext.client import KoomyoClient, check_feed_position
from koomyo_erpnext.exceptions import KoomyoAPIError
from koomyo_erpnext.sync import backfill as engine
from koomyo_erpnext.sync import events, resync
from koomyo_erpnext.tests.test_backfill import BackfillTestCase


class FeedGapCase(BackfillTestCase):
	def feed(self, seqs, deleted_through=None, head=None):
		for seq in seqs:
			self.fake_a.add_event(
				"event_shift_opened", seq=seq, id=f"evt-gap-{seq}", created="2026-10-12T16:52:51Z"
			)
		self.fake_a.deleted_through = deleted_through
		self.fake_a.head = head

	def position(self, last_seq, caught_up="2026-10-10 12:00:00"):
		self.conn_a.db_set({"last_seq": last_seq, "last_caught_up_on": caught_up})
		frappe.db.commit()

	def catch_up(self):
		with self.koomyo():
			return events.catch_up(self.conn_a.name)

	def resyncs(self):
		return frappe.get_all(
			"Koomyo Backfill",
			filters={"connection": self.conn_a.name, "resync_seq": (">", 0)},
			fields=["name", "status", "resync_seq", "from_date", "to_date", "bring_sales", "bring_stock"],
		)


class TestGapSignal(FeedGapCase):
	def test_no_gap_when_the_next_event_is_still_there(self):
		self.feed([4, 5, 6], deleted_through=3)
		self.position(3)
		summary = self.catch_up()
		self.assertTrue(summary["done"], summary)
		self.assertNotIn("gap", summary)
		self.assertEqual(self.resyncs(), [])
		self.assertEqual(frappe.db.get_value("Koomyo Connection", self.conn_a.name, "last_seq"), 6)

	def test_never_a_gap_for_the_first_read(self):
		self.feed([5, 6], deleted_through=4, head=6)
		self.position(0, None)
		summary = self.catch_up()
		self.assertTrue(summary["done"])
		self.assertEqual(self.resyncs(), [])

	def test_an_old_position_is_no_longer_a_guess(self):
		"""31 days since the last read used to stop the feed; now only Koomyo's `gap` does."""
		self.feed([5, 6])
		self.position(4, datetime.now() - timedelta(days=31))
		summary = self.catch_up()
		self.assertTrue(summary["done"], summary)
		self.assertEqual(frappe.db.get_value("Koomyo Connection", self.conn_a.name, "status"), "Connected")

	def test_a_koomyo_without_the_signal_is_read_as_no_gap(self):
		self.fake_a.send_position = False
		self.feed([5, 6])
		self.position(1, datetime.now() - timedelta(days=40))
		summary = self.catch_up()
		self.assertTrue(summary["done"], summary)
		self.assertEqual(self.resyncs(), [])

	def test_bad_position_fields_are_refused(self):
		with self.assertRaises(KoomyoAPIError):
			check_feed_position({"oldest_seq": "3", "latest_seq": 4, "gap": False})
		with self.assertRaises(KoomyoAPIError):
			check_feed_position({"oldest_seq": 3, "latest_seq": 4, "gap": "yes"})
		page = {}
		check_feed_position(page)
		self.assertEqual(page, {"oldest_seq": None, "latest_seq": None, "gap": False})


class TestResync(FeedGapCase):
	def gap(self):
		self.feed([5, 6], deleted_through=4, head=6)
		self.position(2)
		return self.catch_up()

	def test_a_gap_stops_the_feed_alerts_and_starts_a_resync(self):
		with patch("frappe.sendmail") as sendmail:
			self.conn_a.db_set("last_alert_on", None)
			summary = self.gap()
		self.assertTrue(summary["gap"])
		conn = frappe.get_doc("Koomyo Connection", self.conn_a.name)
		self.assertEqual((conn.status, conn.last_seq), ("Needs Re-sync", 2), "the feed did not move on")
		self.assertEqual(
			frappe.db.count("Koomyo Event Log", {"connection": self.conn_a.name, "event_id": "evt-gap-5"}), 0
		)
		(backfill,) = self.resyncs()
		self.assertEqual((backfill.status, backfill.resync_seq, backfill.bring_sales), ("Queued", 6, 1))
		self.assertEqual(str(backfill.from_date), "2026-10-09", "the day before the last complete read")
		self.assertIn("lost events", conn.status_message)
		self.assertTrue(sendmail.called or not frappe.db.get_single_value("Koomyo Settings", "alert_email"))

	def test_the_resync_reads_the_lists_then_the_feed_continues_from_the_head(self):
		self.gap()
		(backfill,) = self.resyncs()
		with self.koomyo():
			engine.run(backfill["name"])
		self.assertEqual({i.koomyo_id for i in self.invoices()}, {"o1", "o2", "o3"})
		conn = frappe.get_doc("Koomyo Connection", self.conn_a.name)
		self.assertEqual((conn.status, conn.last_seq, conn.status_message), ("Connected", 6, ""))
		# a second pass over the same range changes nothing, and the feed carries on from 6
		self.fake_a.deleted_through = None
		self.fake_a.events.append({**self.fake_a.events[-1], "seq": 7, "id": "evt-gap-7"})
		summary = self.catch_up()
		self.assertTrue(summary["done"])
		self.assertEqual(frappe.db.get_value("Koomyo Connection", self.conn_a.name, "last_seq"), 7)

	def test_while_the_resync_runs_the_feed_is_not_read(self):
		self.gap()
		calls = len([c for c in self.fake_a.calls if c["path"] == "/events"])
		self.catch_up()
		self.assertEqual(len([c for c in self.fake_a.calls if c["path"] == "/events"]), calls)
		self.assertEqual(len(self.resyncs()), 1, "a second gap read does not make a second backfill")

	def test_failures_keep_it_open_until_they_are_retried_away(self):
		self.gap()
		(backfill,) = self.resyncs()
		frappe.db.set_value("Koomyo Backfill", backfill["name"], "failed", 2)
		resync.finish_resync(frappe.get_doc("Koomyo Backfill", backfill["name"]))
		conn = frappe.get_doc("Koomyo Connection", self.conn_a.name)
		self.assertEqual(conn.status, "Needs Re-sync")
		self.assertIn("Retry Failed", conn.status_message)
		frappe.db.set_value("Koomyo Backfill", backfill["name"], "failed", 0)
		resync.finish_resync(frappe.get_doc("Koomyo Backfill", backfill["name"]))
		conn.reload()
		self.assertEqual((conn.status, conn.last_seq), ("Connected", 6))

	def test_a_problem_starting_it_is_said_plainly_and_the_backfill_waits(self):
		with patch(
			"koomyo_erpnext.koomyo_erpnext.doctype.koomyo_backfill.koomyo_backfill.closed_period_problem",
			return_value="Accounting Period X is closed.",
		):
			self.gap()
		(backfill,) = self.resyncs()
		self.assertEqual(backfill.status, "Draft")
		conn = frappe.get_doc("Koomyo Connection", self.conn_a.name)
		self.assertEqual(conn.status, "Needs Re-sync")
		self.assertIn("Accounting Period X is closed", conn.status_message)
		self.assertIn("press Start", conn.status_message)

	def test_a_range_longer_than_a_backfill_is_cut_at_a_year(self):
		self.feed([5, 6], deleted_through=4, head=6)
		self.position(2, "2024-01-01 00:00:00")
		self.catch_up()
		(backfill,) = self.resyncs()
		self.assertEqual(
			(frappe.utils.getdate(backfill.to_date) - frappe.utils.getdate(backfill.from_date)).days, 365
		)

	def test_no_head_means_no_automatic_resync(self):
		self.feed([5, 6], deleted_through=4, head=6)
		with patch.object(
			KoomyoClient,
			"get_events",
			return_value={
				"object": "list",
				"data": [],
				"has_more": False,
				"next_after": 2,
				"gap": True,
				"oldest_seq": 5,
				"latest_seq": None,
			},
		):
			self.position(2)
			self.catch_up()
		self.assertEqual(self.resyncs(), [])
		conn = frappe.get_doc("Koomyo Connection", self.conn_a.name)
		self.assertEqual(conn.status, "Needs Re-sync")
		self.assertIn("did not say where it ends", conn.status_message)
