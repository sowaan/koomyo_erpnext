"""Koomyo complaints -> ERPNext Issues.

Docs: docs.koomyo.com/events/complaints, /guides/orders-and-complaints, OpenAPI schema Complaint,
events complaint.created and complaint.updated (scope complaints.read). Only for a restaurant that has
the Complaints app. The event carries the complaint in full except the timeline, photos and
resolutions (those are GET /complaints/{id}).

Each complaint is one Issue, keyed (connection, complaint id). Koomyo is the source of truth for a
complaint, so every event brings the Issue up to the newest copy (an older copy, by `updated_at`,
changes nothing):

- subject: "<ticket number>: <summary>"; description: the customer's or staff's text, escaped, with
  the category, channel, order number, SLA dates and photo count as plain lines;
- status: new / acknowledged / in_progress -> Open, waiting_customer -> On Hold, resolved -> Resolved,
  closed -> Closed; priority: low -> Low, normal -> Medium, high and urgent -> High;
- resolution_details: the resolution summary and what it cost the restaurant;
- customer: only when Koomyo's customer id is a Customer already synced for this connection.

Privacy (CLAUDE.md rule 9): the customer's name and phone are never copied, and nothing else about a
customer is looked up. The summary and description are typed text and can mention a name; that is the
complaint itself and is kept as written, escaped.
"""

import frappe
from frappe import _
from frappe.utils import escape_html, get_datetime

from koomyo_erpnext.exceptions import Fatal
from koomyo_erpnext.sync.common import find_existing, stamp
from koomyo_erpnext.sync.events import handles

SCOPES = ("complaints.read",)
STATUS = {
	"new": "Open",
	"acknowledged": "Open",
	"in_progress": "Open",
	"waiting_customer": "On Hold",
	"resolved": "Resolved",
	"closed": "Closed",
}
PRIORITY = {"low": "Low", "normal": "Medium", "high": "High", "urgent": "High"}


@handles("complaint.created", "complaint.updated", switch="sync_complaints", scopes=SCOPES)
def on_complaint(event: dict, connection):
	return apply_complaint(event["data"], connection)


def apply_complaint(card: dict, connection) -> tuple[str, str]:
	if card.get("object") != "complaint" or not card.get("id"):
		raise Fatal(_("Koomyo sent something that is not a complaint."))
	status = STATUS.get(card.get("status"))
	if not status:
		raise Fatal(
			_("Complaint {0} has status {1!r}.").format(card.get("ticket_number"), card.get("status"))
		)

	name = find_existing("Issue", connection, card["id"])
	if name:
		issue = frappe.get_doc("Issue", name)
		seen, new = issue.get("koomyo_updated_at"), card.get("updated_at")
		if seen and new and get_datetime(new) < get_datetime(seen):
			return ("Issue", issue.name)
	else:
		issue = frappe.new_doc("Issue")
		issue.company = connection.company
		issue.opening_date = card.get("business_date")
		stamp(issue, connection, card["id"], card.get("branch_id"))

	number = card.get("ticket_number") or card["id"]
	issue.subject = f"{number}: {card.get('summary') or ''}"[:140]
	issue.description = description_of(card)
	issue.status = status
	priority = PRIORITY.get(card.get("priority"))
	if priority and frappe.db.exists("Issue Priority", priority):
		issue.priority = priority
	customer = (card.get("customer") or {}).get("id")
	if customer:
		issue.customer = find_existing("Customer", connection, customer)
	if card.get("resolution_summary"):
		cost = card.get("resolution_cost") or "0.00"
		issue.resolution_details = f"{escape_html(card['resolution_summary'])}<br>" + _(
			"Cost to the restaurant: {0}"
		).format(escape_html(str(cost)))
	if issue.is_new():
		issue.insert(ignore_permissions=True)
	else:
		issue.save(ignore_permissions=True)
	issue.db_set("koomyo_updated_at", card.get("updated_at"), update_modified=False)
	return ("Issue", issue.name)


def description_of(card: dict) -> str:
	"""The complaint text and its facts as escaped HTML lines (Koomyo's text is untrusted)."""
	lines = []
	if card.get("description"):
		lines.append(escape_html(card["description"]).replace("\n", "<br>"))
	facts = []

	def add(label, value):
		if value not in (None, ""):
			facts.append(f"{label}: {escape_html(str(value))}")

	category = card.get("category") or {}
	add(_("Category"), category.get("name"))
	add(_("Channel"), card.get("channel"))
	add(_("Priority"), card.get("priority"))
	add(_("Order"), (card.get("order") or {}).get("number"))
	sla = card.get("sla") or {}
	add(_("Acknowledge by"), sla.get("acknowledge_due_at"))
	add(_("Resolve by"), sla.get("resolve_due_at"))
	if sla.get("overdue"):
		facts.append(_("Overdue"))
	if card.get("attachment_count"):
		add(_("Photos in Koomyo"), card["attachment_count"])
	if card.get("reopen_count"):
		add(_("Reopened"), card["reopen_count"])
	if facts:
		lines.append("<br>".join(facts))
	return "<br><br>".join(lines)
