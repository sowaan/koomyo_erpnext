# Copyright (c) 2026, Sowaan and contributors
# For license information, please see license.txt

"""Backend of the Koomyo setup wizard (page `koomyo-setup`) and its onboarding banner.

The wizard reads the current state of a connection with `get_state` and saves each step
through the small functions below. The heavy work is the connection's own methods
(Test Connection, Set Up From Koomyo, Load Menu and Ingredients), so the wizard and the
connection form always behave the same. The API key and webhook secret are never sent
back to the browser: only whether they are saved.
"""

import frappe
from frappe import _

ROLES = ("System Manager", "Koomyo Manager")
CONNECTION = "Koomyo Connection"

SYNC_FIELDS = (
	"sync_sales",
	"sync_refunds",
	"partial_refunds",
	"sync_shifts",
	"sync_stock",
	"stock_deduction",
	"sync_purchasing",
	"book_koomyo_purchases",
	"sync_customers",
	"sync_complaints",
)


def _guard():
	frappe.only_for(ROLES)


def _doc(name: str):
	doc = frappe.get_doc(CONNECTION, name)
	doc.check_permission("write")
	return doc


def connection_state(doc) -> dict:
	"""What the wizard shows about one connection. Secrets are reported only as booleans."""
	from koomyo_erpnext.auto_setup import checklist

	return {
		"name": doc.name,
		"company": doc.company,
		"base_url": doc.base_url,
		"access_mode": doc.access_mode or "Read and Write",
		"start_from_date": str(doc.start_from_date or ""),
		"has_key": bool(doc.get_password("api_key", raise_exception=False)),
		"has_secret": bool(doc.get_password("webhook_secret", raise_exception=False)),
		"status": doc.status,
		"status_message": doc.status_message,
		"restaurant": doc.restaurant_name,
		"currency": doc.koomyo_currency,
		"timezone": doc.koomyo_timezone,
		"scopes": [s.strip() for s in (doc.key_scopes or "").split(",") if s.strip()],
		"missing_scopes": doc.missing_scopes,
		"enabled": bool(doc.enabled),
		"dry_run": bool(doc.dry_run),
		"syncs": {f: doc.get(f) for f in SYNC_FIELDS},
		"webhook_url": doc.webhook_url,
		"events_to_register": doc.events_to_register,
		"maps": {
			"branches": len(doc.branch_map),
			"payments": len(doc.payment_map),
			"taxes": len(doc.tax_map),
			"units": len(doc.uom_map),
			"waste": len(doc.waste_map),
		},
		"checklist": checklist(doc),
		"catalog_summary": doc.catalog_summary,
	}


@frappe.whitelist()
def get_state(connection=None):
	_guard()
	connections = frappe.get_list(
		CONNECTION, fields=["name", "company", "restaurant_name", "status"], order_by="creation desc"
	)
	return {
		"companies": frappe.get_list("Company", pluck="name"),
		"default_company": frappe.defaults.get_global_default("company"),
		"connections": connections,
		"connection": connection_state(_doc(connection)) if connection else None,
		"default_base_url": "https://api.koomyo.com/api/public/v1",
		"today": frappe.utils.today(),
	}


@frappe.whitelist()
def save_connection(
	company, connection_name, base_url, start_from_date, access_mode="Read and Write", api_key=None
):
	"""Step 2: create the connection, or update it. The key is only changed when one is given."""
	_guard()
	if not (company and frappe.db.exists("Company", company)):
		frappe.throw(_("Select a valid Company."))
	connection_name = (connection_name or "").strip()
	if not connection_name:
		frappe.throw(_("Give the connection a name."))
	if frappe.db.exists(CONNECTION, connection_name):
		doc = _doc(connection_name)
		if doc.company != company:
			frappe.throw(_("Connection {0} belongs to Company {1}.").format(connection_name, doc.company))
	else:
		if not api_key:
			frappe.throw(_("Paste the Koomyo API key."))
		doc = frappe.new_doc(CONNECTION)
		doc.connection_name = connection_name
		doc.company = company
	doc.base_url = base_url
	doc.start_from_date = start_from_date
	doc.access_mode = access_mode
	if api_key:
		doc.api_key = api_key
	doc.save()
	return connection_state(doc)


@frappe.whitelist()
def test_connection(connection):
	_guard()
	doc = _doc(connection)
	result = doc.test_connection()
	doc.reload()
	return {"result": result, "state": connection_state(doc)}


@frappe.whitelist()
def plan(connection):
	_guard()
	return _doc(connection).auto_setup_plan()


@frappe.whitelist()
def apply(connection, choices, load_menu=0):
	_guard()
	doc = _doc(connection)
	result = doc.auto_setup_apply(choices, load_menu)
	doc.reload()
	return {"result": result, "state": connection_state(doc)}


@frappe.whitelist()
def save_syncs(connection, values):
	"""Step 4: switch syncs on or off. ERPNext's own checks say what a switched-on sync still needs."""
	_guard()
	doc = _doc(connection)
	for key, value in frappe.parse_json(values).items():
		if key in SYNC_FIELDS:
			doc.set(key, value)
	doc.save()
	return connection_state(doc)


@frappe.whitelist()
def load_menu(connection, preview=0):
	_guard()
	return _doc(connection).load_catalog(preview=preview)


@frappe.whitelist()
def save_webhook_secret(connection, secret=None):
	_guard()
	doc = _doc(connection)
	if secret:
		doc.webhook_secret = secret
		doc.save()
	return connection_state(doc)


@frappe.whitelist()
def finish(connection, go_live=0):
	"""Step 7: switch the connection on, in Dry Run unless the user chose to go live."""
	_guard()
	doc = _doc(connection)
	doc.dry_run = 0 if int(go_live) else 1
	doc.enabled = 1
	doc.save()
	return connection_state(doc)


@frappe.whitelist()
def get_pending_setup():
	"""For the onboarding banner: true while the site has no Koomyo connection at all."""
	if not (frappe.session.user == "Administrator" or set(ROLES) & set(frappe.get_roles())):
		return False
	return not frappe.db.exists(CONNECTION, {})
