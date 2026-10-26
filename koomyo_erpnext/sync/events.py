"""Event intake, dispatch and the feed catch-up.

Docs: docs.koomyo.com/events, /events/feed, /webhooks/reliable.

- Webhook and feed share one door, `store_event`: one Event Log row per
  (connection, event id), so a duplicate delivery or a replay changes nothing.
- `process_pending` runs one job per connection and handles that connection's
  events in `seq` order, so events about the same order or shift run in feed order.
- `catch_up` reads `GET /events?after=<last seq>` per connection and stores what
  it finds through the same door. It saves `after` only once the page is stored.
"""

import json
import traceback
from collections.abc import Callable
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, getdate, now_datetime, strip_html
from frappe.utils.data import convert_utc_to_system_timezone

from koomyo_erpnext.client import KoomyoClient, check_event
from koomyo_erpnext.exceptions import Fatal, KoomyoAPIError, NeedsMapping, Retryable, Skipped
from koomyo_erpnext.utils import redact, redact_text

EVENT_LOG = "Koomyo Event Log"
CONNECTION = "Koomyo Connection"

TEST_EVENT_TYPE = "webhook.test"

# Koomyo keeps events for 30 days (docs: events/feed). If our last full read of the
# feed is older than this, events may have been dropped before we read them.
FEED_SAFE_DAYS = 29
FEED_PAGE_SIZE = 500
FEED_MAX_PAGES_PER_RUN = 20
PROCESS_BATCH = 200

# Scopes every connection needs for what is built so far:
# events.read for the catch-up, catalog.read for the branch list in Test Connection.
BASE_SCOPES = ("events.read", "catalog.read")

Handler = Callable[[dict, "frappe.model.document.Document"], tuple[str, str] | None]

# event type -> handler. Filled by the sync modules with @handles(...).
DISPATCH: dict[str, Handler] = {}
HANDLER_SCOPES: dict[str, tuple[str, ...]] = {}
HANDLER_SWITCH: dict[str, str | None] = {}

# Back-off before automatic retry n (1-based) of a Retryable failure.
RETRY_BACKOFF_MINUTES = (1, 5, 15, 30, 60, 120, 240, 480)


def handles(*event_types: str, switch: str | None = None, scopes: tuple[str, ...] = ()):
	"""Register a handler for one or more event types.

	A handler takes (event, connection) and creates its ERPNext documents in the
	current transaction. It returns (doctype, name) of the main document, or None,
	or raises Retryable / NeedsMapping / Skipped / Fatal.

	`switch` is the Check field on Koomyo Connection that turns the sync on;
	`scopes` are the API key scopes the sync needs (docs: reference/scopes).
	"""

	def decorator(fn: Handler) -> Handler:
		for event_type in event_types:
			DISPATCH[event_type] = fn
			HANDLER_SCOPES[event_type] = tuple(scopes)
			HANDLER_SWITCH[event_type] = switch
		return fn

	return decorator


# Work that starts in ERPNext (a submitted Purchase Order, ...) goes through the same Event
# Log and processor under these types; Koomyo never sends them.
OUTBOUND_PREFIX = "erpnext."


def handled_types() -> list[str]:
	"""Koomyo event types the app handles (what the feed is asked for)."""
	return sorted(t for t in DISPATCH if not t.startswith(OUTBOUND_PREFIX))


def switched_on_types(connection, include_outbound: bool = False) -> list[str]:
	"""Event types this connection books: their sync switch is on."""
	return sorted(
		t
		for t in DISPATCH
		if (include_outbound or not t.startswith(OUTBOUND_PREFIX))
		and (not HANDLER_SWITCH.get(t) or connection.get(HANDLER_SWITCH[t]))
	)


def required_scopes(connection=None) -> list[str]:
	"""Scopes for what is switched on (all handled types when no connection is given)."""
	scopes = set(BASE_SCOPES)
	types = switched_on_types(connection, include_outbound=True) if connection is not None else list(DISPATCH)
	for event_type in types:
		scopes.update(HANDLER_SCOPES.get(event_type, ()))
	return sorted(scopes)


# ----------------------------------------------------------------- intake


class InvalidEvent(Exception):
	"""An event that fails the envelope or tenant checks. The message is safe to show."""


def store_event(connection, event: dict, via: str) -> tuple[str, bool]:
	"""Store one event for `connection`. Returns (event log name, created).

	Checks the envelope shape and that the event belongs to this connection's
	restaurant. A second copy of the same event id returns the first row unchanged.
	"""
	reason = check_event(event)
	if reason:
		raise InvalidEvent(reason)
	if not connection.tenant_id or event["tenant_id"] != connection.tenant_id:
		raise InvalidEvent("event belongs to another Koomyo restaurant")

	existing = frappe.db.get_value(EVENT_LOG, {"connection": connection.name, "event_id": event["id"]})
	if existing:
		return existing, False

	log = frappe.new_doc(EVENT_LOG)
	log.update(
		{
			"connection": connection.name,
			"company": connection.company,
			"event_id": event["id"],
			"event_type": event["type"],
			"seq": event["seq"],
			"received_via": via,
			"event_created": _utc_naive(event.get("created")),
			"business_date": _date_or_none(event.get("business_date")),
			"branch_id": _short_text(event.get("branch_id")),
			"entity_type": _short_text((event.get("entity") or {}).get("type")),
			"entity_id": _short_text((event.get("entity") or {}).get("id")),
			"payload": json.dumps(redact(event), indent=1, sort_keys=True),
			"status": "Pending",
		}
	)
	if event["type"] == TEST_EVENT_TYPE:
		# Docs (webhooks#test-it): accept it and do nothing.
		log.status = "Done"
		log.message = _("Test event received from Koomyo. Nothing to book.")
		log.processed_on = now_datetime()
	elif event["type"] not in DISPATCH:
		log.status = "Skipped"
		log.message = _("This version of the app does not sync {0} events.").format(event["type"])
		log.processed_on = now_datetime()

	frappe.db.savepoint("koomyo_store")
	try:
		log.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		# Another worker stored the same event between our check and insert.
		frappe.db.rollback(save_point="koomyo_store")
		return frappe.db.get_value(EVENT_LOG, {"connection": connection.name, "event_id": event["id"]}), False
	return log.name, True


# ------------------------------------------------------------- processing


def enqueue_processing(connection_name: str):
	"""Start (or keep) the one processing job of this connection."""
	frappe.enqueue(
		"koomyo_erpnext.sync.events.process_pending",
		queue="default",
		job_id=f"koomyo-process-{frappe.local.site}-{connection_name}",
		deduplicate=True,
		enqueue_after_commit=True,
		connection_name=connection_name,
	)


def due_events(connection_name: str, limit: int = PROCESS_BATCH) -> list[str]:
	"""Pending events, and Retryable failures whose wait is over, in feed order."""
	max_retries = max_auto_retries()
	log = frappe.qb.DocType(EVENT_LOG)
	now = now_datetime()
	query = (
		frappe.qb.from_(log)
		.select(log.name)
		.where(log.connection == connection_name)
		.where(
			(log.status == "Pending")
			# A row left in Processing by a worker that died.
			| ((log.status == "Processing") & (log.modified < add_to_date(now, hours=-1)))
			| (
				(log.status == "Failed")
				& (log.error_class == "Retryable")
				& (log.next_retry_on.isnotnull())
				& (log.next_retry_on <= now)
				& (log.attempts < max_retries + 1)
			)
		)
		.orderby(log.seq)
		.orderby(log.creation)
		.limit(limit)
	)
	return [row[0] for row in query.run()]


def process_pending(connection_name: str):
	"""Handle every due event of one connection, oldest `seq` first. Commits per event."""
	if not frappe.db.get_value(CONNECTION, connection_name, "enabled"):
		return
	seen = set()
	while True:
		names = [n for n in due_events(connection_name) if n not in seen]
		if not names:
			return
		for name in names:
			seen.add(name)
			process_event(name)
			frappe.db.commit()


def process_event(log_name: str):
	"""Run the handler for one stored event and record the outcome on its row."""
	log = frappe.get_doc(EVENT_LOG, log_name, for_update=True)
	if log.status not in ("Pending", "Failed", "Processing"):
		return
	connection = frappe.get_doc(CONNECTION, log.connection)
	if not connection.enabled:
		return

	handler = DISPATCH.get(log.event_type)
	if not handler:
		_finish(
			log,
			"Skipped",
			message=_("This version of the app does not sync {0} events.").format(log.event_type),
		)
		return

	switch = HANDLER_SWITCH.get(log.event_type)
	if switch and not connection.get(switch):
		label = _(connection.meta.get_label(switch))
		_finish(
			log,
			"Skipped",
			message=_(
				"{0} is off for this connection. Switch it on and press Retry to book this event."
			).format(label),
		)
		return

	log.attempts = (log.attempts or 0) + 1
	log.status = "Processing"
	log.save(ignore_permissions=True)
	frappe.db.commit()

	event = json.loads(log.payload)
	frappe.db.savepoint("koomyo_event")
	dry_run = bool(connection.dry_run)
	try:
		result = handler(event, connection)
		if dry_run:
			raise DryRunDone(result)
	except DryRunWrite as e:
		frappe.db.rollback(save_point="koomyo_event")
		log.dry_run = 1
		_finish(log, "Skipped", message=_("Dry run: {0}. Nothing was sent to Koomyo.").format(e))
	except DryRunDone as e:
		what = e.describe()  # read before the rollback removes it
		frappe.db.rollback(save_point="koomyo_event")
		log.dry_run = 1
		_finish(
			log, "Skipped", message=_("Dry run: this event would make {0}. Nothing was saved.").format(what)
		)
	except Skipped as e:
		frappe.db.rollback(save_point="koomyo_event")
		_finish(log, "Skipped", message=str(e))
	except NeedsMapping as e:
		frappe.db.rollback(save_point="koomyo_event")
		_finish(log, "Failed", error_class="NeedsMapping", message=str(e))
	except (Retryable, KoomyoAPIError) as e:
		frappe.db.rollback(save_point="koomyo_event")
		if isinstance(e, KoomyoAPIError) and not e.is_retryable:
			_finish(log, "Failed", error_class="Fatal", message=str(e))
		else:
			_fail_retryable(log, str(e))
	except Fatal as e:
		frappe.db.rollback(save_point="koomyo_event")
		_finish(log, "Failed", error_class="Fatal", message=str(e))
	except frappe.ValidationError as e:
		# ERPNext refused the document (closed period, a missing default, ...): its message says why.
		frappe.db.rollback(save_point="koomyo_event")
		_finish(
			log,
			"Failed",
			error_class="Fatal",
			message=_("ERPNext refused it: {0}").format(strip_html(str(e)) or type(e).__name__),
			error=traceback.format_exc(),
		)
	except Exception as e:
		frappe.db.rollback(save_point="koomyo_event")
		_finish(
			log,
			"Failed",
			error_class="Fatal",
			message=_("Unexpected error: {0}").format(type(e).__name__),
			error=traceback.format_exc(),
		)
	else:
		log.dry_run = 0
		reference = result if isinstance(result, tuple | list) and len(result) == 2 else (None, None)
		_finish(log, "Done", reference=reference)
	finally:
		# Messages ERPNext printed while building documents mean nothing to a background job.
		frappe.local.message_log = []


class DryRunWrite(Exception):
	"""An outbound handler in dry-run mode stops before its first write to Koomyo."""


def koomyo_write(connection, client, method: str, path: str, body: dict | None, idempotency_key: str) -> dict:
	"""Every write to Koomyo goes through here: dry run stops it, and it always carries an Idempotency-Key."""
	if connection.dry_run:
		raise DryRunWrite(_("would send {0} {1}").format(method, path))
	return client.request(method, path, json=body, idempotency_key=idempotency_key[:200])


def idempotency_key(doctype: str, name: str, modified, action: str = "") -> str:
	"""Stable per document version: a retry of the same version replays, a new version is a new write."""
	stamp_ = get_datetime(modified).strftime("%Y%m%d%H%M%S%f")
	return "-".join(p for p in ("erpnext", action or frappe.scrub(doctype), name, stamp_) if p)[:200]


def store_outbound(
	connection, event_type: str, doc, action: str = "", extra: dict | None = None
) -> str | None:
	"""Queue work that starts in ERPNext. One row per document version (the idempotency key)."""
	key = idempotency_key(doc.doctype, doc.name, doc.modified, action)
	if frappe.db.exists(EVENT_LOG, {"connection": connection.name, "event_id": key}):
		return None
	log = frappe.new_doc(EVENT_LOG)
	log.update(
		{
			"connection": connection.name,
			"company": connection.company,
			"event_id": key,
			"event_type": event_type,
			"seq": 0,
			"received_via": "ERPNext",
			"event_created": now_datetime(),
			"business_date": getdate(),
			"entity_type": doc.doctype,
			"entity_id": doc.name,
			"payload": json.dumps(
				{
					"type": event_type,
					"data": {"doctype": doc.doctype, "name": doc.name, "key": key, **(extra or {})},
				}
			),
			"status": "Pending",
		}
	)
	log.insert(ignore_permissions=True)
	enqueue_processing(connection.name)
	return log.name


class DryRunDone(Exception):
	"""Raised after a handler succeeded in dry-run mode, so everything it made is rolled back."""

	def __init__(self, result):
		self.result = result
		super().__init__("dry run")

	def describe(self) -> str:
		if not (isinstance(self.result, tuple | list) and len(self.result) == 2 and self.result[0]):
			return _("no document")
		doctype, name = self.result
		if frappe.get_meta(doctype).has_field("grand_total"):
			total = frappe.db.get_value(doctype, name, "grand_total")
			return _("a {0} for {1}").format(_(doctype), frappe.format(total, "Currency"))
		return _("a {0}").format(_(doctype))


def _fail_retryable(log, message: str):
	attempt = log.attempts or 1
	max_retries = max_auto_retries()
	if attempt > max_retries:
		_finish(
			log,
			"Failed",
			error_class="Retryable",
			message=_("{0} Gave up after {1} tries. Press Retry when the cause is fixed.").format(
				message, attempt
			),
		)
		return
	wait = RETRY_BACKOFF_MINUTES[min(attempt, len(RETRY_BACKOFF_MINUTES)) - 1]
	_finish(
		log,
		"Failed",
		error_class="Retryable",
		message=message,
		next_retry_on=add_to_date(now_datetime(), minutes=wait),
	)


def _finish(log, status, *, error_class="", message="", error="", reference=(None, None), next_retry_on=None):
	log.status = status
	log.error_class = error_class
	log.message = redact_text(message)[:1000]
	log.error = redact_text(error)
	log.next_retry_on = next_retry_on
	log.processed_on = now_datetime()
	if reference and reference[0]:
		log.reference_doctype, log.reference_name = reference
	log.save(ignore_permissions=True)


def max_auto_retries() -> int:
	value = frappe.db.get_single_value("Koomyo Settings", "max_auto_retries")
	return 8 if value is None else int(value)


# ---------------------------------------------------------------- retries


def reset_for_retry(log) -> bool:
	if log.status not in ("Failed", "Skipped"):
		return False
	log.status = "Pending"
	log.error_class = ""
	log.message = ""
	log.error = ""
	log.next_retry_on = None
	log.attempts = 0
	log.save(ignore_permissions=True)
	return True


# ---------------------------------------------------------------- catch-up


def schedule_catch_up():
	"""Scheduler entry: one catch-up job and one processing job per enabled connection.

	Each connection gets its own job id, so a slow or broken connection never
	holds up another one.
	"""
	for name in frappe.get_all(CONNECTION, filters={"enabled": 1}, pluck="name"):
		frappe.enqueue(
			"koomyo_erpnext.sync.events.catch_up",
			queue="long",
			job_id=f"koomyo-catchup-{frappe.local.site}-{name}",
			deduplicate=True,
			connection_name=name,
		)
		enqueue_processing(name)


def catch_up(connection_name: str, client: KoomyoClient | None = None) -> dict:
	"""Read the events feed for one connection from its saved `last_seq`.

	Returns a small summary (used by tests and the Catch Up Now button).
	"""
	connection = frappe.get_doc(CONNECTION, connection_name)
	summary = {"stored": 0, "duplicates": 0, "pages": 0, "done": False}
	if not connection.enabled or connection.status == "Needs Re-sync":
		return summary
	types = handled_types()
	if not types:
		# Nothing in this version reads events yet; nothing to fetch.
		summary["done"] = True
		return summary

	if connection.last_caught_up_on and get_datetime(
		connection.last_caught_up_on
	) < now_datetime() - timedelta(days=FEED_SAFE_DAYS):
		mark_connection(
			connection,
			"Needs Re-sync",
			_(
				"The events feed was last read completely on {0}. Koomyo keeps events for 30 days, so some may be gone. "
				"Bring that period in with a backfill, then press Restart Feed."
			).format(frappe.format(connection.last_caught_up_on, "Datetime")),
		)
		return summary

	client = client or KoomyoClient.for_connection(connection)
	after = int(connection.last_seq or 0)
	since = None if (after or connection.last_caught_up_on) else str(getdate(connection.start_from_date))

	try:
		while summary["pages"] < FEED_MAX_PAGES_PER_RUN:
			page = client.get_events(after=after, since=since, types=types, limit=FEED_PAGE_SIZE)
			summary["pages"] += 1
			if page["next_after"] < after:
				raise KoomyoAPIError(None, "bad_response", _("Koomyo's events feed went backwards."))
			for event in page["data"]:
				if isinstance(event.get("seq"), int) and event["seq"] <= after:
					raise KoomyoAPIError(
						None, "bad_response", _("Koomyo's events feed repeated an older event number.")
					)
				try:
					_name, created = store_event(connection, event, "Feed")
				except InvalidEvent as e:
					raise KoomyoAPIError(
						None, "bad_response", _("Koomyo's events feed sent an invalid event: {0}").format(e)
					)
				summary["stored" if created else "duplicates"] += 1
			after = page["next_after"]
			since = None
			# Save the position in the same transaction as the events of this page.
			connection.db_set("last_seq", after, update_modified=False)
			if not page["has_more"]:
				connection.db_set("last_caught_up_on", now_datetime(), update_modified=False)
				summary["done"] = True
			frappe.db.commit()
			if summary["done"]:
				break
	except KoomyoAPIError as e:
		frappe.db.rollback()
		if e.is_retryable:
			# Koomyo is down or busy. The next scheduled run carries on from the saved position.
			frappe.logger("koomyo_erpnext", allow_site=True).warning("catch-up %s: %s", connection_name, e)
			summary["error"] = str(e)
		else:
			mark_connection(connection, "Needs Attention", _("Reading the events feed failed. {0}").format(e))
			summary["error"] = str(e)
		return summary

	if connection.status == "Needs Attention":
		mark_connection(connection, "Connected", "")
	enqueue_processing(connection_name)
	return summary


# ------------------------------------------------------------ health and alerts


def mark_connection(connection, status: str, message: str):
	"""Set a connection's health and send the daily alert when it needs a person."""
	connection.reload()
	connection.db_set(
		{"status": status, "status_message": redact_text(message)[:1000]}, update_modified=False
	)
	if status in ("Needs Attention", "Needs Re-sync"):
		send_alert(connection)
	frappe.db.commit()


def send_alert(connection):
	"""Email the alert address once a day per connection, never more."""
	today = getdate()
	if connection.last_alert_on and getdate(connection.last_alert_on) >= today:
		return
	recipient = frappe.db.get_single_value("Koomyo Settings", "alert_email")
	connection.db_set("last_alert_on", today, update_modified=False)
	if not recipient:
		return
	frappe.sendmail(
		recipients=[recipient],
		subject=_("Koomyo connection {0} needs attention").format(connection.name),
		message=_("{0}<br><br>Status: {1}<br>Open the connection in ERPNext to fix it: {2}").format(
			frappe.utils.escape_html(connection.status_message or ""),
			frappe.utils.escape_html(connection.status),
			frappe.utils.get_url_to_form(CONNECTION, connection.name),
		),
		delayed=True,
	)


def daily_alerts():
	for name in frappe.get_all(
		CONNECTION,
		filters={"enabled": 1, "status": ["in", ["Needs Attention", "Needs Re-sync"]]},
		pluck="name",
	):
		send_alert(frappe.get_doc(CONNECTION, name))
	frappe.db.commit()


def cleanup_event_log():
	"""Delete Done and Skipped rows older than the retention setting. Failed rows stay."""
	days = frappe.db.get_single_value("Koomyo Settings", "event_log_retention_days")
	days = 30 if days is None else int(days)
	if days <= 0:
		return
	cutoff = add_to_date(now_datetime(), days=-days)
	frappe.db.delete(EVENT_LOG, {"status": ("in", ("Done", "Skipped")), "modified": ("<", cutoff)})
	frappe.db.commit()


# ---------------------------------------------------------------- helpers


def _utc_naive(value):
	"""RFC 3339 UTC text -> naive datetime in the site's time zone (how Frappe stores datetimes)."""
	if not isinstance(value, str) or not value:
		return None
	try:
		moment = get_datetime(value.replace("Z", "+00:00"))
		if moment.tzinfo is None:
			return None
		return convert_utc_to_system_timezone(moment.replace(tzinfo=None)).replace(tzinfo=None)
	except Exception:
		return None


def _date_or_none(value):
	if not isinstance(value, str) or not value:
		return None
	try:
		return getdate(value)
	except Exception:
		return None


def _short_text(value, length: int = 140):
	return value[:length] if isinstance(value, str) else None


# Register the handlers of every sync (each module calls @handles at import time).
from koomyo_erpnext.sync import customers, purchasing, refunds, sales, shifts, stock, transfers
