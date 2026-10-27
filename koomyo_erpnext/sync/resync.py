"""What to do when the events feed has lost events (`gap` is true).

Docs: docs.koomyo.com/events/feed#how-long-events-are-kept. Koomyo keeps events 30 days. Every
answer of GET /events says `oldest_seq`, `latest_seq` and `gap` (events between our `after` and
`oldest_seq` were deleted). `gap` is never true for `after=0`.

On a gap the feed is not read on: carrying on from `next_after` would hide the hole. Instead:

1. the connection is marked Needs Re-sync and the alert goes out (once a day);
2. a Koomyo Backfill is made for the days since the last complete read, for everything that is
   switched on, and started: it reads the list endpoints, which hold the full history, through the
   same handlers, so nothing is doubled;
3. when that backfill completes without failures, the feed continues from `latest_seq` and the
   connection is Connected again. If the backfill cannot start (a closed accounting period, for
   instance) or ends with failures, the connection stays on Needs Re-sync and the message says what
   to do; fixing it and pressing Start, or Retry Failed, on that backfill finishes the job.
"""

import frappe
from frappe import _
from frappe.utils import add_days, getdate, now_datetime, today

MAX_DAYS = 365  # one backfill covers at most 366 days


def start_resync(connection, oldest_seq, latest_seq) -> str | None:
	"""Mark the connection, then make and start the backfill. Returns the backfill's name."""
	from koomyo_erpnext.sync.events import mark_connection

	if not isinstance(latest_seq, int):
		# No head to continue from: nothing safe to do automatically.
		mark_connection(
			connection,
			"Needs Re-sync",
			_(
				"Koomyo's events feed lost events (it now starts at {0}) and did not say where it ends. Bring the missing days in with a backfill, then press Restart Feed."
			).format(oldest_seq),
		)
		return None

	existing = frappe.db.get_value(
		"Koomyo Backfill",
		{"connection": connection.name, "resync_seq": (">", 0), "status": ("not in", ("Completed",))},
		"name",
	)
	start = last_good_date(connection)
	end = getdate(today())
	if (end - start).days > MAX_DAYS:
		start = add_days(end, -MAX_DAYS)
	message = _(
		"Koomyo's events feed has lost events: it now starts at event {0} and ours was older. The days since {1} are being read again from Koomyo's lists, then the feed continues from event {2}."
	).format(oldest_seq, frappe.format(start, "Date"), latest_seq)
	if existing:
		frappe.db.set_value("Koomyo Backfill", existing, "resync_seq", latest_seq)
		mark_connection(connection, "Needs Re-sync", message)
		return existing

	mark_connection(connection, "Needs Re-sync", message)
	backfill = frappe.new_doc("Koomyo Backfill")
	ticks = ticks_for(connection, start)
	backfill.update(
		{
			"connection": connection.name,
			"company": connection.company,
			"from_date": start,
			"to_date": end,
			"resync_seq": latest_seq,
			**ticks,
		}
	)
	try:
		backfill.insert(ignore_permissions=True)
	except frappe.ValidationError as e:
		frappe.clear_last_message()
		note_problem(
			connection, _("The re-sync could not be prepared: {0}").format(frappe.utils.strip_html(str(e)))
		)
		return None
	try:
		backfill.flags.ignore_permissions = True
		backfill.start()
	except frappe.ValidationError as e:
		frappe.clear_last_message()
		note_problem(
			connection,
			_(
				"The re-sync is ready as backfill {0} but could not start: {1} Fix that, then press Start on it."
			).format(backfill.name, frappe.utils.strip_html(str(e))),
		)
	frappe.db.commit()
	return backfill.name


def last_good_date(connection):
	"""The day to read again from: the day before the last complete read of the feed."""
	if connection.last_caught_up_on:
		return add_days(getdate(connection.last_caught_up_on), -1)
	return getdate(connection.start_from_date or today())


def ticks_for(connection, start) -> dict:
	ticks = {
		"bring_sales": int(bool(connection.sync_sales or connection.sync_refunds)),
		"bring_shifts": int(bool(connection.sync_shifts)),
		"bring_customers": int(bool(connection.sync_customers)),
		"bring_purchases": int(bool(connection.sync_purchasing)),
		# Stock before the opening stock date is already in the opening stock.
		"bring_stock": int(
			bool(
				connection.sync_stock
				and connection.stock_start_date
				and getdate(start) >= getdate(connection.stock_start_date)
			)
		),
	}
	if not any(ticks.values()):
		ticks["bring_sales"] = 1
	return ticks


def note_problem(connection, message: str):
	connection.db_set("status_message", message[:1000], update_modified=False)


def finish_resync(backfill):
	"""Called when a backfill ends: if it was a feed re-sync that went through, continue the feed."""
	if not backfill.get("resync_seq"):
		return
	connection = frappe.get_doc("Koomyo Connection", backfill.connection)
	failed = frappe.db.get_value("Koomyo Backfill", backfill.name, "failed") or 0
	if failed:
		note_problem(
			connection,
			_(
				"The re-sync backfill {0} finished with {1} failed record(s). Open it, press Retry Failed, and the feed continues when none are left."
			).format(backfill.name, failed),
		)
		return
	connection.db_set(
		{
			"last_seq": backfill.resync_seq,
			"last_caught_up_on": now_datetime(),
			"status": "Connected",
			"status_message": "",
		},
		update_modified=False,
	)
	frappe.db.commit()
