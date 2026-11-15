"""Quantities from Koomyo are in the ingredient's own unit, whatever purchase UOM the Item has.

Seen live: Ice Cube is `pcs` in Koomyo with a purchase unit of kg (66 pcs a kg). A request for 1320 pcs was booked as
1320 Kg (87,120 pieces) because the row had no unit and ERPNext filled in the Item's purchase UOM.
"""

import frappe
from frappe.utils import flt

from koomyo_erpnext.tests import test_purchase_orders as po_tests
from koomyo_erpnext.tests import test_purchase_requests as pr_tests
from koomyo_erpnext.tests.test_stock import MOZZARELLA

ICE = "73403093-3258-45ed-aa4e-bc1058620370"


class TestRequestRowUnit(pr_tests.RequestCase):
	def test_a_request_in_pieces_is_booked_in_pieces_not_in_the_purchase_unit(self):
		record = self.fake_a.add_ingredient(ICE, "Ice Cube", "pcs", "1.0000")
		record.update({"purchase_unit": "kg", "purchase_unit_factor": "66"})
		ice = {"id": ICE, "name": "Ice Cube", "unit": "pcs"}
		card = pr_tests.make_request(lines=[pr_tests.line(pr_tests.LINE_1, ice, "1320.000", "1320.000")])
		log = self.send(card)
		self.assertEqual(log.status, "Done", (log.message, (log.error or "")[-400:]))
		(row,) = self.mr().items
		item = frappe.get_doc("Item", row.item_code)
		# the Item holds the pack, as before: Nos is the stock unit, Kg the purchase UOM at 66 a kg
		self.assertEqual((item.stock_uom, item.purchase_uom), ("Nos", "Kg"))
		self.assertEqual({r.uom: r.conversion_factor for r in item.uoms}["Kg"], 66)
		# but the row is what Koomyo asked for
		self.assertEqual(
			(row.uom, flt(row.qty, 3), flt(row.conversion_factor), flt(row.stock_qty, 3)),
			("Nos", 1320, 1, 1320),
		)


class TestOrderRowUnit(po_tests.PurchaseOrderCase):
	def test_a_koomyo_order_row_stays_in_the_stock_unit_when_the_item_has_a_purchase_uom(self):
		code = frappe.db.get_value("Item", {"koomyo_connection": self.conn_a.name, "koomyo_id": MOZZARELLA})
		item = frappe.get_doc("Item", code)
		item.append("uoms", {"uom": "Box", "conversion_factor": 25})
		item.purchase_uom = "Box"
		item.save()
		log = self.send(po_tests.make_order(), "purchase.created")
		self.assertEqual(log.status, "Done", (log.message, (log.error or "")[-400:]))
		(row,) = self.po().items
		self.assertEqual(
			(row.uom, flt(row.qty, 3), flt(row.conversion_factor), flt(row.stock_qty, 3)), ("Kg", 10, 1, 10)
		)
		self.assertEqual((flt(row.rate, 2), flt(row.amount, 2)), (1450, 14500))
