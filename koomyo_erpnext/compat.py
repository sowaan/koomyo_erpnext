"""Small shims so one branch runs on Frappe v15 and v16."""

try:  # Frappe v16
	from frappe.tests import IntegrationTestCase as KoomyoTestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as KoomyoTestCase

__all__ = ["KoomyoTestCase"]
