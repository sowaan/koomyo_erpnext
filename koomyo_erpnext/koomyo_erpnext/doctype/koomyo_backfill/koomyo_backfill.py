# Copyright (c) 2026, Sowaan and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, date_diff, getdate, now_datetime, today

MAX_DAYS = 366
# What a backfill can create, for the closed-period check.
POSTED_DOCTYPES = {
	"bring_sales": ("Sales Invoice", "Payment Entry"),
	"bring_shifts": ("Journal Entry",),
	"bring_stock": ("Stock Entry", "Stock Reconciliation"),
	"bring_purchases": ("Purchase Receipt",),
}
# tick -> (connection switches it needs, label)
NEEDS = {
	"bring_sales": (("sync_sales", "sync_refunds"), "Sales and Refunds"),
	"bring_shifts": (("sync_shifts",), "Shifts"),
	"bring_stock": (("sync_stock",), "Stock Movements and Transfers"),
	"bring_customers": (("sync_customers",), "Customers"),
	"bring_purchases": (("sync_purchasing",), "Purchases"),
}


class KoomyoBackfill(Document):
	def validate(self):
		if self.status in ("Queued", "Running", "Previewing", "Stopping") and not self.flags.from_job:
			if self.has_value_changed("from_date") or self.has_value_changed("to_date"):
				frappe.throw(_("This backfill is running. Stop it before changing it."))
		self.validate_range()
		self.validate_connection()
		if (
			self.is_new()
			or self.has_value_changed("from_date")
			or self.has_value_changed("to_date")
			or ticks_changed(self)
		):
			self.make_chunks()

	def validate_range(self):
		if getdate(self.to_date) > getdate(today()):
			frappe.throw(_("To Date cannot be in the future."))
		if getdate(self.from_date) > getdate(self.to_date):
			frappe.throw(_("From Date must be on or before To Date."))
		if date_diff(self.to_date, self.from_date) >= MAX_DAYS:
			frappe.throw(
				_("A backfill covers at most {0} days. Split the range into several backfills.").format(
					MAX_DAYS
				)
			)
		if not any(self.get(tick) for tick in NEEDS):
			frappe.throw(_("Tick at least one thing to bring."))

	def validate_connection(self):
		connection = frappe.get_doc("Koomyo Connection", self.connection)
		if not connection.enabled:
			frappe.throw(_("Connection {0} is switched off. Switch it on first.").format(connection.name))
		for tick, (switches, label) in NEEDS.items():
			if self.get(tick):
				off = [connection.meta.get_label(s) for s in switches if not connection.get(s)]
				if off:
					frappe.throw(
						_("{0} needs {1} switched on in connection {2}.").format(
							_(label), ", ".join(_(o) for o in off), connection.name
						)
					)
		if self.bring_stock and getdate(self.from_date) < getdate(connection.stock_start_date):
			frappe.throw(
				_(
					"Stock can only be brought in from the Opening Stock Date ({0}) on: earlier movements are already in the opening stock. Start the range on or after {0}, or untick Stock."
				).format(frappe.format(connection.stock_start_date, "Date"))
			)
		if self.koomyo_branch_id and not any(
			r.koomyo_branch_id == self.koomyo_branch_id for r in connection.branch_map
		):
			frappe.throw(
				_("Koomyo branch {0} is not on connection {1}'s Branch Map.").format(
					self.koomyo_branch_id, connection.name
				)
			)

	def make_chunks(self):
		self.set("chunks", [])
		if self.bring_sales or self.bring_shifts or self.bring_stock:
			day = getdate(self.from_date)
			while day <= getdate(self.to_date):
				self.append("chunks", {"chunk": str(day), "day": day, "status": "Pending"})
				day = add_days(day, 1)
		if self.bring_customers:
			self.append("chunks", {"chunk": "customers", "status": "Pending"})
		if self.bring_purchases:
			self.append("chunks", {"chunk": "purchases", "status": "Pending"})
		for key in ("found", "created", "already_there", "skipped", "failed", "progress"):
			self.set(key, 0)

	# ------------------------------------------------------------ actions

	@frappe.whitelist()
	def preview(self):
		self.check_can_run()
		self.reset_chunks()
		self.queue(dry_run=True)
		return _("Preview started. It counts and checks, and writes nothing.")

	@frappe.whitelist()
	def start(self):
		self.check_can_run()
		if self.mode == "Dry Run" or self.status in ("Draft", "Previewed"):
			self.reset_chunks()
		self.db_set({"started_by": frappe.session.user, "started_on": now_datetime(), "finished_on": None})
		self.queue(dry_run=False)
		return _("Backfill started. It carries on from where it was if it was stopped.")

	@frappe.whitelist()
	def stop(self):
		self.check_permission("write")
		if self.status not in ("Queued", "Running", "Previewing"):
			frappe.throw(_("The backfill is not running."))
		self.db_set("status", "Stopping")
		return _("Stopping after the record in hand. Press Start to carry on later.")

	@frappe.whitelist()
	def retry_failed(self):
		self.check_can_run()
		if not frappe.db.exists("Koomyo Event Log", {"backfill": self.name, "status": "Failed"}):
			frappe.throw(_("Nothing failed in this backfill."))
		self.queue(dry_run=False, retry_failed=True)
		return _("Retrying the failed records.")

	@frappe.whitelist()
	def rerun_day(self, chunk: str):
		self.check_can_run()
		row = next((c for c in self.chunks if c.name == chunk or c.chunk == chunk), None)
		if not row:
			frappe.throw(_("No such day in this backfill."))
		frappe.db.set_value(
			"Koomyo Backfill Chunk",
			row.name,
			{
				"status": "Pending",
				"state": None,
				"found": 0,
				"created": 0,
				"already_there": 0,
				"skipped": 0,
				"failed": 0,
				"message": None,
			},
		)
		self.queue(dry_run=False, only_chunk=row.name)
		return _("Running {0} again.").format(row.chunk)

	def check_can_run(self):
		self.check_permission("write")
		if self.is_new():
			frappe.throw(_("Save the backfill first."))
		if self.status in ("Queued", "Running", "Previewing", "Stopping"):
			frappe.throw(_("The backfill is already running."))
		self.validate_range()
		self.validate_connection()
		problem = closed_period_problem(self)
		if problem:
			frappe.throw(problem)

	def reset_chunks(self):
		for row in self.chunks:
			frappe.db.set_value(
				"Koomyo Backfill Chunk",
				row.name,
				{
					"status": "Pending",
					"state": None,
					"found": 0,
					"created": 0,
					"already_there": 0,
					"skipped": 0,
					"failed": 0,
					"message": None,
				},
			)
		self.db_set(
			{
				"found": 0,
				"created": 0,
				"already_there": 0,
				"skipped": 0,
				"failed": 0,
				"progress": 0,
				"preview_sample": None,
			}
		)

	def queue(self, dry_run: bool, only_chunk: str | None = None, retry_failed: bool = False):
		self.db_set("status", "Queued")
		frappe.enqueue(
			"koomyo_erpnext.sync.backfill.run",
			queue="long",
			timeout=6 * 3600,
			job_id=f"koomyo-backfill-{frappe.local.site}-{self.name}",
			deduplicate=True,
			enqueue_after_commit=True,
			backfill_name=self.name,
			dry_run=dry_run,
			only_chunk=only_chunk,
			retry_failed=retry_failed,
		)


def ticks_changed(doc) -> bool:
	return any(doc.has_value_changed(tick) for tick in NEEDS) or doc.has_value_changed("koomyo_branch_id")


def closed_period_problem(backfill) -> str | None:
	"""A date the range would post into that ERPNext has closed or frozen, named."""
	company = frappe.db.get_value("Koomyo Connection", backfill.connection, "company")
	start, end = getdate(backfill.from_date), getdate(backfill.to_date)
	doctypes = [dt for tick, dts in POSTED_DOCTYPES.items() if backfill.get(tick) for dt in dts]

	period = frappe.qb.DocType("Accounting Period")
	closed = frappe.qb.DocType("Closed Document")
	rows = (
		frappe.qb.from_(period)
		.join(closed)
		.on(closed.parent == period.name)
		.select(period.name, period.start_date, period.end_date, closed.document_type)
		.where(period.company == company)
		.where(period.start_date <= end)
		.where(period.end_date >= start)
		.where(closed.closed == 1)
		.where(closed.document_type.isin(doctypes or [""]))
		.run(as_dict=True)
	)
	if rows:
		row = rows[0]
		return _("Accounting Period {0} ({1} to {2}) is closed for {3}. Pick a range outside it.").format(
			row.name, row.start_date, row.end_date, _(row.document_type)
		)

	closing = frappe.get_all(
		"Period Closing Voucher",
		filters={"company": company, "docstatus": 1, "period_end_date": (">=", start)},
		fields=["name", "period_end_date"],
		order_by="period_end_date desc",
		limit=1,
	)
	if closing and (
		backfill.bring_sales or backfill.bring_shifts or backfill.bring_purchases or backfill.bring_stock
	):
		return _(
			"The books are closed up to {0} by Period Closing Voucher {1}. Start the range after that day."
		).format(closing[0].period_end_date, closing[0].name)

	frozen = frappe.db.get_single_value("Accounts Settings", "acc_frozen_upto")
	if (
		frozen
		and getdate(frozen) >= start
		and (
			backfill.bring_sales or backfill.bring_shifts or backfill.bring_purchases or backfill.bring_stock
		)
	):
		return _("Accounts are frozen up to {0} (Accounts Settings). Start the range after that day.").format(
			frozen
		)

	stock_frozen = frappe.db.get_single_value("Stock Settings", "stock_frozen_upto")
	if stock_frozen and getdate(stock_frozen) >= start and (backfill.bring_stock or backfill.bring_purchases):
		return _("Stock is frozen up to {0} (Stock Settings). Start the range after that day.").format(
			stock_frozen
		)
	return None


@frappe.whitelist()
def create_for_all_connections(
	from_date, to_date, bring_sales=0, bring_shifts=0, bring_stock=0, bring_customers=0, bring_purchases=0
):
	"""One backfill per enabled connection the user may see (never one mixed run)."""
	if not frappe.has_permission("Koomyo Backfill", "create"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	made, refused = [], []
	for connection in frappe.get_list("Koomyo Connection", filters={"enabled": 1}, pluck="name"):
		doc = frappe.new_doc("Koomyo Backfill")
		doc.update(
			{
				"connection": connection,
				"from_date": from_date,
				"to_date": to_date,
				"bring_sales": int(bring_sales),
				"bring_shifts": int(bring_shifts),
				"bring_stock": int(bring_stock),
				"bring_customers": int(bring_customers),
				"bring_purchases": int(bring_purchases),
			}
		)
		try:
			doc.insert()
			made.append(doc.name)
		except frappe.ValidationError as e:
			frappe.clear_last_message()
			refused.append(f"{connection}: {frappe.utils.strip_html(str(e))}")
	return {"made": made, "refused": refused}
