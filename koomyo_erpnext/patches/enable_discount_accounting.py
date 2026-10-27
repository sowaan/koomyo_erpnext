import frappe


def execute():
	frappe.db.set_single_value("Selling Settings", "enable_discount_accounting", 1)
