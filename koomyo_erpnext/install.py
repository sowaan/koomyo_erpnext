import frappe

KOOMYO_MANAGER = "Koomyo Manager"


def before_install():
	ensure_role()


def after_install():
	ensure_role()


def before_migrate():
	ensure_role()


def ensure_role():
	"""The role must exist before the DocType permissions that name it are synced."""
	if frappe.db.exists("Role", KOOMYO_MANAGER):
		return
	frappe.get_doc({"doctype": "Role", "role_name": KOOMYO_MANAGER, "desk_access": 1, "is_custom": 0}).insert(
		ignore_permissions=True
	)
