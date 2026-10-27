"""Small shims so one branch runs on Frappe v15 and v16."""

try:  # Frappe v16
	from frappe.tests import IntegrationTestCase as KoomyoTestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as KoomyoTestCase

__all__ = ["KoomyoTestCase"]


def payment_entry_for(*args, **kwargs):
	"""ERPNext's `get_payment_entry` without a permission check, as the app's background jobs need.

	v15 takes `ignore_permissions`. v16 dropped it and checks permissions itself, so the call runs as
	Administrator and the previous user is put back afterwards (same effect as ignoring permissions).
	"""
	import inspect

	import frappe
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	if "ignore_permissions" in inspect.signature(get_payment_entry).parameters:
		return get_payment_entry(*args, ignore_permissions=True, **kwargs)
	user = frappe.session.user
	frappe.set_user("Administrator")
	try:
		return get_payment_entry(*args, **kwargs)
	finally:
		frappe.set_user(user)
