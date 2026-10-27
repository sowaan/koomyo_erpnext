"""Backfill: bring a past date range in from Koomyo's list endpoints (not the 30-day feed).

Every record found is wrapped in the same envelope as its live event and stored and
processed through the same Event Log and handlers. So the mapping, the
(koomyo_connection, koomyo_id) guard and the error handling are the live sync's own:
a backfill never doubles what came by webhook, and running a range twice changes nothing.

A run is chunked per day (customers and purchases are one chunk each, as their lists have
no day filter). Each chunk saves its position (Koomyo's opaque next_cursor per list)
after every page, so a Stop, a crash or a restart carries on where it was.
Calls are paced on the X-RateLimit headers and wait out a 429.

Docs: docs.koomyo.com/conventions (pagination, business dates), /events/feed (30 days),
OpenAPI list endpoints /orders, /refunds, /shifts, /stock-movements, /waste, /stock-counts,
/stock-transfers, /customers, /purchase-orders.
"""

import json
import time
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import add_days, getdate, now_datetime

from koomyo_erpnext.client import KoomyoClient, check_list_page
from koomyo_erpnext.exceptions import KoomyoAPIError
from koomyo_erpnext.sync import events
from koomyo_erpnext.sync.common import find_existing, money

BACKFILL = "Koomyo Backfill"
SAMPLE_SIZE = 10
SLEEP = time.sleep


class StopRequested(Exception):
	pass


# ------------------------------------------------------------------ what a record becomes


def kinds_for(backfill) -> list[str]:
	kinds = []
	if backfill.bring_sales:
		kinds += ["order", "refund"]
	if backfill.bring_shifts:
		kinds.append("shift")
	if backfill.bring_stock:
		kinds += ["usage", "waste", "stock_count", "stock_transfer", "adjustment"]
	return kinds


# kind -> (event type, ERPNext doctype holding the koomyo id, koomyo id of a record)
KINDS = {
	"order": ("order.paid", "Sales Invoice", lambda r: r["id"]),
	"refund": ("refund.created", "Sales Invoice", lambda r: r["id"]),
	"shift": ("shift.closed", "Journal Entry", lambda r: r["id"]),
	"usage": ("stock.usage_daily", "Stock Entry", lambda r: f"usage:{r['branch_id']}:{r['business_date']}"),
	"waste": ("waste.recorded", "Stock Entry", lambda r: r["id"]),
	"stock_count": ("stock_count.closed", "Stock Reconciliation", lambda r: r["id"]),
	"stock_transfer": ("stock_transfer.completed", "Stock Entry", lambda r: r["id"]),
	"adjustment": ("stock.adjusted", "Stock Entry", lambda r: r["id"]),
	"customer": ("customer.updated", "Customer", lambda r: r["id"]),
	"purchase_order": ("purchase.updated", "Purchase Order", lambda r: r["id"]),
}


def purchase_already_there(connection, order: dict) -> bool:
	"""A purchase order needs nothing when ERPNext holds its latest copy and every delivery."""
	from koomyo_erpnext.sync.purchase_orders import deliveries, is_stale

	po_name = find_existing("Purchase Order", connection, order["id"])
	if order.get("status") == "draft":
		return True  # a plan: nothing is ever booked for it
	if order.get("status") == "cancelled":
		return not po_name or frappe.db.get_value("Purchase Order", po_name, "docstatus") == 2
	if not po_name:
		return False
	po = frappe.get_doc("Purchase Order", po_name)
	if not po.get("koomyo_updated_at") or not (
		is_stale(po, order) or po.koomyo_updated_at == order.get("updated_at")
	):
		return False
	return all(
		find_existing("Purchase Receipt", connection, r["id"])
		or find_existing("Purchase Receipt", connection, order["id"])
		for r in deliveries(order)
	)


def already_there(connection, kind: str, record: dict) -> bool:
	_type, doctype, key = KINDS[kind]
	if kind == "purchase_order":
		return purchase_already_there(connection, record)
	koomyo_id = key(record)
	if kind == "stock_transfer":
		from koomyo_erpnext.sync.transfers import fully_booked

		return fully_booked(record, connection)
	return bool(find_existing(doctype, connection, koomyo_id))


def envelope(connection, kind: str, record: dict) -> dict:
	"""The live event this record would have come in, so the same handler books it."""
	event_type, _doctype, key = KINDS[kind]
	return {
		"id": f"backfill:{kind}:{key(record)}",
		"object": "event",
		"api_version": "v1",
		"type": event_type,
		"seq": 0,
		"created": record.get("created_at") or record.get("received_at") or "",
		"tenant_id": connection.tenant_id,
		"branch_id": record.get("branch_id"),
		"business_date": record.get("business_date"),
		"timezone": connection.koomyo_timezone,
		"currency": record.get("currency") or connection.koomyo_currency,
		"entity": {"type": kind, "id": record.get("id") or key(record)},
		"data": record,
	}


# ------------------------------------------------------------------ reading Koomyo


class Reader:
	"""Paged list reads for one backfill, with pacing and 429 handling."""

	def __init__(self, connection, client: KoomyoClient | None = None):
		self.connection = connection
		self.client = client or KoomyoClient.for_connection(connection)

	def page(self, path: str, params: dict) -> dict:
		while True:
			try:
				page = self.client.get(path, params)
			except KoomyoAPIError as e:
				if e.code == "rate_limited":
					SLEEP((e.retry_after or 5) + 1)
					continue
				raise
			check_list_page(page, cursor_field="next_cursor")
			self.pace()
			return page

	def pace(self):
		"""Slow and steady: when the allowance runs low, wait for it to refill a little."""
		info = self.client.last_rate_limit or {}
		try:
			limit, remaining = int(info.get("limit")), int(info.get("remaining"))
		except (TypeError, ValueError):
			return
		if limit > 0 and remaining < max(2, limit // 10):
			SLEEP(60.0 / limit * (max(2, limit // 10) - remaining + 1))

	def utc_range(self, day) -> tuple[str, str]:
		"""The restaurant's calendar day as UTC instants; `*_after` filters are exclusive, so start 1s early."""
		zone = ZoneInfo(self.connection.koomyo_timezone or "UTC")
		start = datetime.combine(getdate(day), datetime.min.time(), zone)
		end = start + timedelta(days=1)
		fmt = "%Y-%m-%dT%H:%M:%SZ"
		utc = ZoneInfo("UTC")
		return (start - timedelta(seconds=1)).astimezone(utc).strftime(fmt), end.astimezone(utc).strftime(fmt)

	def lists_for(self, kind: str, day, branch_id: str | None):
		"""(path, params, keep) for one kind on one day. `keep` filters rows the API cannot."""
		after, before = self.utc_range(day) if day else (None, None)
		day_text = str(getdate(day)) if day else None
		on_day = lambda r: r.get("business_date") == day_text  # noqa: E731
		branch = {"branch_id": branch_id} if branch_id else {}
		if kind == "order":
			return [
				(
					"/orders",
					{"business_date": day_text, "payment_status": "paid", "limit": 200, **branch},
					lambda r: (r.get("payment") or {}).get("status") == "paid",
				)
			]
		if kind == "refund":
			# Two days of UTC margin either side; the business_date filter picks the day.
			return [
				(
					"/refunds",
					{
						"created_after": shift_utc(after, -1),
						"created_before": shift_utc(before, 1),
						"limit": 200,
						**branch,
					},
					on_day,
				)
			]
		if kind == "shift":
			return [
				(
					"/shifts",
					{
						"status": "closed",
						"opened_after": shift_utc(after, -1),
						"opened_before": shift_utc(before, 1),
						"limit": 200,
						**branch,
					},
					on_day,
				)
			]
		if kind == "waste":
			return [
				(
					"/waste",
					{
						"created_after": shift_utc(after, -1),
						"created_before": shift_utc(before, 1),
						"limit": 500,
						**branch,
					},
					on_day,
				)
			]
		if kind == "adjustment":
			# What a person adjusted by hand (kind=manual). Counts and waste have their own events and
			# kinds, and an opening stock upload is already in the opening stock.
			own = ("stock_count", "non_sale_usage", "opening_stock")
			return [
				(
					"/stock-movements",
					{
						"kind": "manual",
						"created_after": shift_utc(after, -1),
						"created_before": shift_utc(before, 1),
						"limit": 500,
						**branch,
					},
					lambda r: on_day(r) and r.get("reason") not in own,
				)
			]
		if kind == "stock_count":
			return [
				(
					"/stock-counts",
					{"status": "closed", "created_after": shift_utc(after, -31), "limit": 200, **branch},
					on_day,
				)
			]
		if kind == "stock_transfer":

			def keep(r):
				ends = {(r.get("from") or {}).get("id"), (r.get("to") or {}).get("id")}
				return on_day(r) and (not branch_id or branch_id in ends)

			return [
				(
					"/stock-transfers",
					{"status": "received", "sent_after": shift_utc(after, -31), "limit": 200},
					keep,
				)
			]
		if kind == "usage":
			# The same objects as stock.usage_daily, one per branch that used ingredients, for the day.
			return [("/stock-usage", {"business_date": day_text, "limit": 200, **branch}, lambda r: True)]
		raise ValueError(kind)


def shift_utc(text: str, days: int) -> str:
	moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ") + timedelta(days=days)
	return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ the job


def run(backfill_name: str, dry_run: bool = False, only_chunk: str | None = None, retry_failed: bool = False):
	"""Background job: preview (dry_run) or run one backfill, chunk by chunk."""
	backfill = frappe.get_doc(BACKFILL, backfill_name)
	connection = frappe.get_doc("Koomyo Connection", backfill.connection)
	if dry_run:
		# Same handlers, but nothing is written to Koomyo and every document is rolled back.
		connection.dry_run = 1
	reader = Reader(connection)
	backfill.db_set(
		{
			"status": "Previewing" if dry_run else "Running",
			"error": None,
			"mode": "Dry Run" if dry_run else "Run",
		}
	)
	frappe.db.commit()
	sample: list = []
	try:
		if retry_failed:
			retry_failed_logs(backfill, connection)
		for chunk in backfill.chunks:
			if only_chunk and chunk.name != only_chunk:
				continue
			if not dry_run and chunk.status == "Done" and not only_chunk:
				continue
			run_chunk(backfill, connection, reader, chunk, dry_run, sample)
			update_totals(backfill)
	except StopRequested:
		backfill.db_set({"status": "Stopped", "finished_on": now_datetime()})
		frappe.db.commit()
		return
	except Exception as e:
		frappe.db.rollback()
		if isinstance(e, KoomyoAPIError):
			from koomyo_erpnext.koomyo_erpnext.doctype.koomyo_connection.koomyo_connection import (
				connection_error_message,
			)

			message = connection_error_message(e) + " " + _("Press Start to carry on from where it stopped.")
		else:
			message = _("Stopped by an unexpected error: {0}").format(frappe.utils.strip_html(str(e)))
			frappe.log_error(title=f"Koomyo backfill {backfill_name}")
		# The day it was on did not finish: say so there too.
		frappe.db.set_value(
			"Koomyo Backfill Chunk",
			{"parent": backfill_name, "status": "Running"},
			{"status": "Failed", "message": message[:1000]},
		)
		backfill.db_set({"status": "Failed", "error": message[:1000], "finished_on": now_datetime()})
		frappe.db.commit()
		return
	update_totals(backfill)
	if dry_run:
		backfill.db_set(
			{
				"status": "Previewed",
				"preview_sample": json.dumps(sample, indent=1),
				"finished_on": now_datetime(),
			}
		)
	else:
		backfill.db_set({"status": "Completed", "finished_on": now_datetime()})
	frappe.db.commit()
	if not dry_run:
		from koomyo_erpnext.sync.resync import finish_resync

		finish_resync(backfill)


def run_chunk(backfill, connection, reader, chunk, dry_run: bool, sample: list):
	state = {} if dry_run else json.loads(chunk.state or "{}")
	counts = (
		{"found": 0, "created": 0, "already_there": 0, "skipped": 0, "failed": 0}
		if dry_run or not state
		else state.get("counts", {})
	)
	for key in ("found", "created", "already_there", "skipped", "failed"):
		counts.setdefault(key, 0)
	messages: list[str] = []
	set_chunk(chunk, status="Running")

	def handle(kind: str, record: dict):
		check_stop(backfill)
		counts["found"] += 1
		if already_there(connection, kind, record):
			counts["already_there"] += 1
			return
		if len(sample) < SAMPLE_SIZE:
			sample.append(describe(kind, record))
		outcome, message = book(connection, kind, record, backfill.name, dry_run)
		counts[outcome] += 1
		if message and len(messages) < 5:
			messages.append(message)

	for kind in chunk_kinds(backfill, chunk):
		if kind in state.get("done", []):
			continue
		if kind in ("customer", "purchase_order"):
			read_unpaged_kind(backfill, reader, kind, state, handle, chunk, counts, dry_run)
		elif kind == "usage":
			from koomyo_erpnext.sync.order_stock import PER_ORDER, effective_mode

			if effective_mode(connection, chunk.day) == PER_ORDER:
				messages.append(
					_(
						"{0}: Stock Deduction is Per Order, so ingredients come with each order (tick Sales and Refunds)."
					).format(chunk.day)
				)
			else:
				for path, params, _keep in reader.lists_for("usage", chunk.day, backfill.koomyo_branch_id):
					try:
						usages = read_all(reader, path, params)
					except KoomyoAPIError as e:
						if e.code != "day_not_settled":
							raise
						# Not empty: Koomyo gives a day's usage only once it is final (an hour after the day ends).
						counts["skipped"] += 1
						messages.append(
							_(
								"Usage of {0} is not settled in Koomyo yet (day_not_settled). Run this day again later."
							).format(chunk.day)
						)
						continue
					for usage in usages:
						handle("usage", usage)
		else:
			for path, params, keep in reader.lists_for(kind, chunk.day, backfill.koomyo_branch_id):
				cursor_key = f"{kind}:{path}:{params.get('reason', '')}"
				cursor = state.get("cursors", {}).get(cursor_key)
				while True:
					page = reader.page(path, {**params, **({"cursor": cursor} if cursor else {})})
					for record in page["data"]:
						if keep(record):
							handle(kind, record)
					cursor = page["next_cursor"] if page["has_more"] else None
					if not dry_run:
						state.setdefault("cursors", {})[cursor_key] = cursor
						save_state(chunk, state, counts)
					if not cursor:
						break
		state.setdefault("done", []).append(kind)
		if not dry_run:
			save_state(chunk, state, counts)

	status = "Failed" if counts["failed"] else "Done"
	set_chunk(chunk, status=status, message="\n".join(messages)[:1000], **counts)
	if not dry_run:
		save_state(chunk, {**state, "done": state.get("done", [])}, counts)


def read_unpaged_kind(backfill, reader, kind, state, handle, chunk, counts, dry_run):
	"""Customers (by last change) and purchase orders (all received ones), filtered to the range."""
	start, end = str(getdate(backfill.from_date)), str(getdate(backfill.to_date))
	if kind == "customer":
		path, params = "/customers", {"updated_since": start, "limit": 500}
		keep = lambda r: (r.get("updated_at") or "")[:10] <= end  # noqa: E731
	else:
		path, params = "/purchase-orders", {"status": "received", "limit": 200}
		if backfill.koomyo_branch_id:
			params["branch_id"] = backfill.koomyo_branch_id
		keep = lambda r: start <= (r.get("business_date") or "") <= end  # noqa: E731
	cursor = state.get("cursors", {}).get(kind)
	while True:
		page = reader.page(path, {**params, **({"cursor": cursor} if cursor else {})})
		for record in page["data"]:
			if keep(record):
				handle(kind, record)
		cursor = page["next_cursor"] if page["has_more"] else None
		if not dry_run:
			state.setdefault("cursors", {})[kind] = cursor
			save_state(chunk, state, counts)
		if not cursor:
			break


def read_all(reader, path, params) -> list[dict]:
	rows, cursor = [], None
	while True:
		page = reader.page(path, {**params, **({"cursor": cursor} if cursor else {})})
		rows += page["data"]
		cursor = page["next_cursor"] if page["has_more"] else None
		if not cursor:
			return rows


def chunk_kinds(backfill, chunk) -> list[str]:
	if chunk.chunk == "customers":
		return ["customer"]
	if chunk.chunk == "purchases":
		return ["purchase_order"]
	return kinds_for(backfill)


def book(connection, kind: str, record: dict, backfill_name: str, dry_run: bool) -> tuple[str, str | None]:
	"""Store and process one record like a live event. Returns (count bucket, message)."""
	event = envelope(connection, kind, record)
	if dry_run:
		return book_dry(connection, event)
	try:
		log_name, created = events.store_event(connection, event, "Backfill")
	except events.InvalidEvent as e:
		return "failed", str(e)
	log = frappe.get_doc(events.EVENT_LOG, log_name)
	if not created and log.status in ("Done",):
		return "already_there", None
	if not log.backfill:
		log.db_set("backfill", backfill_name, update_modified=False)
	if log.status in ("Failed", "Skipped"):
		events.reset_for_retry(log)
	frappe.db.commit()
	events.process_event(log_name)
	frappe.db.commit()
	return outcome_of(log_name)


def outcome_of(log_name: str) -> tuple[str, str | None]:
	status, error_class, message = frappe.db.get_value(
		events.EVENT_LOG, log_name, ["status", "error_class", "message"]
	)
	if status == "Done":
		return "created", None
	if status == "Skipped" or error_class == "NeedsMapping":
		return "skipped", message
	return "failed", message


def book_dry(connection, event: dict) -> tuple[str, str | None]:
	"""Run the handler and roll everything back: what would happen, with nothing written."""
	handler = events.DISPATCH.get(event["type"])
	frappe.db.savepoint("koomyo_backfill_preview")
	try:
		handler(event, connection)
		return "created", None
	except events.DryRunWrite:
		return "created", None
	except (events.Skipped, events.NeedsMapping) as e:
		return "skipped", str(e)
	except Exception as e:
		return "failed", frappe.utils.strip_html(str(e))[:300]
	finally:
		frappe.db.rollback(save_point="koomyo_backfill_preview")
		frappe.local.message_log = []


def describe(kind: str, record: dict) -> dict:
	amounts = record.get("amounts") or {}
	return {
		"kind": kind,
		"id": record.get("id") or KINDS[kind][2](record),
		"number": record.get("number") or record.get("order_number") or record.get("name"),
		"business_date": record.get("business_date"),
		"amount": amounts.get("amount_due")
		or record.get("amount")
		or record.get("value")
		or record.get("total_received"),
	}


def retry_failed_logs(backfill, connection):
	for name in frappe.get_all(
		events.EVENT_LOG,
		filters={"backfill": backfill.name, "status": "Failed"},
		pluck="name",
		order_by="creation asc",
	):
		check_stop(backfill)
		events.reset_for_retry(frappe.get_doc(events.EVENT_LOG, name))
		frappe.db.commit()
		events.process_event(name)
		frappe.db.commit()
	recount_from_logs(backfill)


def recount_from_logs(backfill):
	"""After a retry, move the retried rows from failed to their new bucket in each chunk."""
	for chunk in backfill.chunks:
		if not chunk.failed:
			continue
		state = json.loads(chunk.state or "{}")
		counts = state.get("counts") or {}
		still_failed = (
			frappe.db.count(
				events.EVENT_LOG, {"backfill": backfill.name, "status": "Failed", "business_date": chunk.day}
			)
			if chunk.day
			else frappe.db.count(
				events.EVENT_LOG,
				{"backfill": backfill.name, "status": "Failed", "business_date": ("is", "not set")},
			)
		)
		fixed = max(0, (counts.get("failed") or 0) - still_failed)
		counts["failed"] = still_failed
		counts["created"] = (counts.get("created") or 0) + fixed
		save_state(chunk, state, counts)
		set_chunk(chunk, status="Failed" if still_failed else "Done", **counts)


# ------------------------------------------------------------------ bookkeeping


def check_stop(backfill):
	if frappe.db.get_value(BACKFILL, backfill.name, "status") == "Stopping":
		raise StopRequested()


def set_chunk(chunk, **values):
	frappe.db.set_value("Koomyo Backfill Chunk", chunk.name, values, update_modified=False)
	chunk.update(values)
	frappe.db.commit()


def save_state(chunk, state: dict, counts: dict):
	state["counts"] = counts
	frappe.db.set_value(
		"Koomyo Backfill Chunk", chunk.name, {"state": json.dumps(state), **counts}, update_modified=False
	)
	frappe.db.commit()


def update_totals(backfill):
	rows = frappe.get_all(
		"Koomyo Backfill Chunk",
		filters={"parent": backfill.name},
		fields=["status", "found", "created", "already_there", "skipped", "failed"],
	)
	totals = {
		k: sum(r[k] or 0 for r in rows) for k in ("found", "created", "already_there", "skipped", "failed")
	}
	finished = sum(1 for r in rows if r.status in ("Done", "Failed"))
	totals["progress"] = round(100 * finished / len(rows), 1) if rows else 100
	backfill.db_set(totals, update_modified=False)
	frappe.db.commit()
