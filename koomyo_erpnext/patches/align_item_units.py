"""Align the Items of every connection to their Koomyo ingredient's unit and pack size.

A unit changed in Koomyo was never pulled into ERPNext (sync/item_units.py). This queues the healing of each
connection in the background (Koomyo is read there, not during migrate). It is safe to run again: an aligned Item
is left alone, an Item with transactions is flagged once, and nothing is ever written to Koomyo.
"""

import frappe


def execute():
	from koomyo_erpnext.sync.item_units import queue_heal

	if not frappe.db.table_exists("Koomyo Connection"):
		return
	for name in frappe.get_all("Koomyo Connection", filters={"enabled": 1, "sync_stock": 1}, pluck="name"):
		queue_heal(name)
