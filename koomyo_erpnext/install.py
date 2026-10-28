import frappe

KOOMYO_MANAGER = "Koomyo Manager"

# Every doctype the app writes carries koomyo_connection + koomyo_id (fixtures/custom_field.json).
KOOMYO_DOCTYPES = (
	"Sales Invoice",
	"POS Invoice",
	"Payment Entry",
	"Customer",
	"Item",
	"Supplier",
	"Address",
	"Contact",
	"Purchase Order",
	"Material Request",
	"Purchase Receipt",
	"Landed Cost Voucher",
	"Stock Entry",
	"Stock Reconciliation",
	"Journal Entry",
	"Issue",
)


def before_install():
	ensure_role()


def after_install():
	ensure_role()
	add_unique_indexes()
	allow_negative_stock()
	enable_discount_accounting()


def before_migrate():
	ensure_role()


def after_sync():
	"""On install the custom fields exist only now, so the unique indexes can be made."""
	add_unique_indexes()


def after_migrate():
	add_unique_indexes()


def ensure_role():
	"""The role must exist before the DocType permissions that name it are synced."""
	if frappe.db.exists("Role", KOOMYO_MANAGER):
		return
	frappe.get_doc({"doctype": "Role", "role_name": KOOMYO_MANAGER, "desk_access": 1, "is_custom": 0}).insert(
		ignore_permissions=True
	)


def add_unique_indexes():
	"""(koomyo_connection, koomyo_id) is unique per doctype: the database refuses a second copy.

	Rows that did not come from Koomyo have NULL in both columns, which a unique index allows.
	"""
	for doctype in KOOMYO_DOCTYPES:
		if not frappe.db.has_column(doctype, "koomyo_id") or not frappe.db.has_column(
			doctype, "koomyo_connection"
		):
			continue
		frappe.db.add_unique(
			doctype, ["koomyo_connection", "koomyo_id"], constraint_name="unique_koomyo_record"
		)


def allow_negative_stock():
	"""Koomyo allows negative stock (it sells before a purchase is entered); ERPNext refuses by default.

	Stock Settings is site-wide, so this is switched on once at install and never switched off.
	"""
	frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)


def enable_discount_accounting():
	"""Koomyo discounts are posted per line with a discount account (list price, discount, net price)."""
	frappe.db.set_single_value("Selling Settings", "enable_discount_accounting", 1)
