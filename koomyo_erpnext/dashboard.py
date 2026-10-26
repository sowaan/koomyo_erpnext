"""Data for the Koomyo Dashboard page: one card per connection the user may see."""

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

EVENT_LOG = "Koomyo Event Log"
STATUSES = ("Pending", "Processing", "Done", "Skipped", "Failed")
RUNNING_BACKFILL = ("Queued", "Running", "Previewing", "Stopping")


def has_app_permission() -> bool:
	"""Show the Koomyo tile on /apps to the people who run the integration."""
	return bool({"System Manager", "Koomyo Manager"} & set(frappe.get_roles()))


@frappe.whitelist()
def get_dashboard() -> dict:
	if not frappe.has_permission("Koomyo Connection", "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	since = add_to_date(now_datetime(), hours=-24)
	connections = []
	# get_list applies roles and User Permissions on Company: the other Companies stay hidden.
	for conn in frappe.get_list(
		"Koomyo Connection",
		fields=[
			"name",
			"company",
			"restaurant_name",
			"enabled",
			"status",
			"status_message",
			"last_seq",
			"last_caught_up_on",
			"dry_run",
		],
		order_by="name asc",
	):
		filters = {"connection": conn.name}
		counts = {status: frappe.db.count(EVENT_LOG, {**filters, "status": status}) for status in STATUSES}
		last_day = {
			status: frappe.db.count(EVENT_LOG, {**filters, "status": status, "creation": (">=", since)})
			for status in ("Done", "Skipped", "Failed")
		}
		needs_mapping = frappe.db.count(
			EVENT_LOG, {**filters, "status": "Failed", "error_class": "NeedsMapping"}
		)
		last_event = frappe.db.get_value(
			EVENT_LOG, filters, ["creation", "event_type"], order_by="creation desc", as_dict=True
		)
		oldest_pending = frappe.db.get_value(
			EVENT_LOG, {**filters, "status": "Pending"}, "creation", order_by="creation asc"
		)
		backfills = frappe.get_list(
			"Koomyo Backfill",
			filters={"connection": conn.name, "status": ("in", RUNNING_BACKFILL)},
			fields=["name", "status", "progress", "from_date", "to_date"],
		)
		connections.append(
			{
				**conn,
				"counts": counts,
				"last_day": last_day,
				"needs_mapping": needs_mapping,
				"last_event": last_event,
				"oldest_pending": oldest_pending,
				"backfills": backfills,
			}
		)
	totals = {
		"connections": len(connections),
		"attention": sum(
			1
			for c in connections
			if c["enabled"] and c["status"] in ("Needs Attention", "Needs Re-sync", "Not Tested")
		),
		"failed": sum(c["counts"]["Failed"] for c in connections),
		"pending": sum(c["counts"]["Pending"] + c["counts"]["Processing"] for c in connections),
		"done_today": sum(c["last_day"]["Done"] for c in connections),
	}
	return {"totals": totals, "connections": connections, "as_of": now_datetime()}
