import json
import time

import frappe

from koomyo_erpnext.tests.fake_koomyo import load_fixture
from koomyo_erpnext.tests.helpers import (
	SECRET_A,
	SECRET_B,
	TENANT_A,
	TENANT_B,
	KoomyoIntegrationTestCase,
	event_body,
	post_webhook,
	sign,
)


class TestWebhook(KoomyoIntegrationTestCase):
	def order_paid(self, tenant=TENANT_A, **overrides):
		event = load_fixture("event_order_paid")
		event["tenant_id"] = tenant
		event.update(overrides)
		return event

	def test_good_signature_stores_and_queues(self):
		event = self.order_paid()
		body = event_body(event)
		status, text = post_webhook(self.conn_a.webhook_token, body, sign(SECRET_A, body), event["id"])
		self.assertEqual((status, text), (200, "ok"))
		rows = self.logs(self.conn_a.name)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].event_id, event["id"])
		self.assertEqual(rows[0].received_via, "Webhook")
		self.assertEqual(rows[0].company, self.conn_a.company)
		self.assertEqual(rows[0].status, "Pending")
		# Sync Sales is off on this connection: processing skips it, visibly and replayably.
		from koomyo_erpnext.sync import events

		events.process_pending(self.conn_a.name)
		row = self.logs(self.conn_a.name)[0]
		self.assertEqual(row.status, "Skipped")
		self.assertIn("Sync Sales is off", row.message)

	def test_handled_type_is_pending_and_enqueued(self):
		from koomyo_erpnext.sync import events

		events.DISPATCH["order.paid"] = lambda e, c: None
		self.addCleanup(events.DISPATCH.pop, "order.paid", None)
		event = self.order_paid()
		body = event_body(event)
		self.assertEqual(post_webhook(self.conn_a.webhook_token, body, sign(SECRET_A, body))[0], 200)
		self.assertEqual(self.logs(self.conn_a.name)[0].status, "Pending")
		jobs = [kw for _a, kw in self.enqueued if kw.get("connection_name") == self.conn_a.name]
		self.assertTrue(jobs and jobs[0]["deduplicate"])

	def test_wrong_secret_refused(self):
		body = event_body(self.order_paid())
		self.assertEqual(post_webhook(self.conn_a.webhook_token, body, sign("whsec_wrong", body))[0], 401)
		self.assertEqual(self.logs(), [])

	def test_old_timestamp_refused(self):
		body = event_body(self.order_paid())
		old = int(time.time()) - 301
		self.assertEqual(post_webhook(self.conn_a.webhook_token, body, sign(SECRET_A, body, old))[0], 401)
		self.assertEqual(self.logs(), [])

	def test_missing_signature_refused(self):
		body = event_body(self.order_paid())
		self.assertEqual(post_webhook(self.conn_a.webhook_token, body, None)[0], 401)

	def test_replayed_event_id_changes_nothing(self):
		event = self.order_paid()
		body = event_body(event)
		for _ in range(3):
			self.assertEqual(post_webhook(self.conn_a.webhook_token, body, sign(SECRET_A, body))[0], 200)
		rows = self.logs(self.conn_a.name)
		self.assertEqual(len(rows), 1)

	def test_payload_signed_for_a_refused_by_b(self):
		event = self.order_paid(tenant=TENANT_A)
		body = event_body(event)
		self.assertEqual(post_webhook(self.conn_b.webhook_token, body, sign(SECRET_A, body))[0], 401)
		self.assertEqual(self.logs(), [])

	def test_event_of_other_restaurant_refused_even_if_signed(self):
		event = self.order_paid(tenant=TENANT_A)
		body = event_body(event)
		self.assertEqual(post_webhook(self.conn_b.webhook_token, body, sign(SECRET_B, body))[0], 400)
		self.assertEqual(self.logs(), [])

	def test_same_event_id_in_two_connections_is_two_rows(self):
		event_a = self.order_paid(tenant=TENANT_A)
		event_b = self.order_paid(tenant=TENANT_B)
		self.assertEqual(event_a["id"], event_b["id"])
		for conn, event, secret in ((self.conn_a, event_a, SECRET_A), (self.conn_b, event_b, SECRET_B)):
			body = event_body(event)
			self.assertEqual(post_webhook(conn.webhook_token, body, sign(secret, body))[0], 200)
		self.assertEqual(len(self.logs(self.conn_a.name)), 1)
		self.assertEqual(len(self.logs(self.conn_b.name)), 1)

	def test_unknown_token_and_disabled_connection(self):
		body = event_body(self.order_paid())
		self.assertEqual(post_webhook("nope", body, sign(SECRET_A, body))[0], 404)
		self.assertEqual(post_webhook("", body, sign(SECRET_A, body))[0], 404)
		self.conn_a.db_set("enabled", 0)
		self.assertEqual(post_webhook(self.conn_a.webhook_token, body, sign(SECRET_A, body))[0], 503)
		self.assertEqual(self.logs(), [])

	def test_bad_bodies(self):
		for body in (b"not json", b"[]", json.dumps({"id": "x"}).encode()):
			self.assertEqual(post_webhook(self.conn_a.webhook_token, body, sign(SECRET_A, body))[0], 400)
		event = self.order_paid()
		body = event_body(event)
		self.assertEqual(
			post_webhook(self.conn_a.webhook_token, body, sign(SECRET_A, body), "other-id")[0], 400
		)
		self.assertEqual(self.logs(), [])

	def test_webhook_test_event_done_without_work(self):
		event = load_fixture("event_webhook_test")
		event["tenant_id"] = TENANT_A
		body = event_body(event)
		self.assertEqual(post_webhook(self.conn_a.webhook_token, body, sign(SECRET_A, body))[0], 200)
		row = self.logs(self.conn_a.name)[0]
		self.assertEqual(row.status, "Done")
		self.assertEqual(self.enqueued, [])

	def test_works_as_guest(self):
		frappe.set_user("Guest")
		event = self.order_paid()
		body = event_body(event)
		self.assertEqual(post_webhook(self.conn_a.webhook_token, body, sign(SECRET_A, body))[0], 200)
		frappe.set_user("Administrator")
		self.assertEqual(len(self.logs(self.conn_a.name)), 1)

	def test_stored_payload_has_no_secrets(self):
		event = self.order_paid()
		event["data"]["secret"] = "whsec_should_not_be_kept"
		body = event_body(event)
		post_webhook(self.conn_a.webhook_token, body, sign(SECRET_A, body))
		payload = self.logs(self.conn_a.name)[0].payload
		self.assertNotIn("whsec_should_not_be_kept", payload)
		self.assertNotIn(SECRET_A, payload)
