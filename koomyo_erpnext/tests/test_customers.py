"""Slice 5: customer.created / customer.updated -> Customer (name, phone, email only)."""

import copy

import frappe

from koomyo_erpnext.tests.fake_koomyo import load_fixture
from koomyo_erpnext.tests.helpers import TENANT_A, TENANT_B, configure_sales
from koomyo_erpnext.tests.test_sales import SalesTestCase, order_event


def customer_event(seq, tenant=TENANT_A, kind="customer.created", **data):
	event = load_fixture("event_customer_created")
	event.update({"tenant_id": tenant, "seq": seq, "id": f"evt-cust-{seq}", "type": kind})
	event["data"].update(data)
	event["entity"]["id"] = event["data"]["id"]
	return event


class TestCustomers(SalesTestCase):
	def setUp(self):
		super().setUp()
		for conn in (self.conn_a, self.conn_b):
			conn.reload()
			conn.update(
				{"sync_customers": 1, "customer_group": "_Koomyo Customers", "territory": "_Koomyo Territory"}
			)
			conn.save()

	def tearDown(self):
		mine = (self.conn_a.name, self.conn_b.name)
		super().tearDown()  # invoices and payments first: they link to the customers
		for name in frappe.get_all("Customer", filters={"koomyo_connection": ("in", mine)}, pluck="name"):
			contact = frappe.db.get_value("Customer", name, "customer_primary_contact")
			frappe.delete_doc("Customer", name, force=True, ignore_permissions=True)
			if contact:
				frappe.delete_doc("Contact", contact, force=True, ignore_permissions=True)
		frappe.db.commit()

	def customer(self, koomyo_id, connection=None):
		name = frappe.db.get_value(
			"Customer", {"koomyo_connection": (connection or self.conn_a).name, "koomyo_id": koomyo_id}
		)
		return frappe.get_doc("Customer", name) if name else None

	def test_docs_example_copies_only_name_phone_email(self):
		event = customer_event(1)
		data = event["data"]
		log = self.book(event)
		self.assertEqual(log.status, "Done", log.message)
		customer = self.customer(data["id"])
		self.assertEqual(customer.customer_name, data["name"])
		self.assertEqual(
			(customer.customer_group, customer.territory), ("_Koomyo Customers", "_Koomyo Territory")
		)
		contact = frappe.get_doc("Contact", customer.customer_primary_contact)
		self.assertEqual((contact.mobile_no, contact.email_id), (data["phone"], data["email"]))
		stored = frappe.as_json(customer.as_dict()) + frappe.as_json(contact.as_dict())
		for private in ("vip", data.get("notes") or "~none~"):
			self.assertNotIn(private, stored)
		self.assertNotIn('"tags"', log.payload, "tags never reach the Event Log either")
		(ref,) = self.fake_a.refs
		self.assertEqual(
			(ref["entity_type"], ref["entity_id"], ref["external_id"]),
			("customer", data["id"], customer.name),
		)

	def test_update_and_late_older_event(self):
		data_id = load_fixture("event_customer_created")["data"]["id"]
		self.book(customer_event(1))
		self.assertEqual(
			self.book(customer_event(3, kind="customer.updated", name="Ayesha K.")).status, "Done"
		)
		self.assertEqual(self.customer(data_id).customer_name, "Ayesha K.")
		# A retried delivery of an older change arrives late: it must not undo the newer one.
		late = customer_event(2, kind="customer.updated", name="Old Name")
		from koomyo_erpnext.sync import events

		with self.koomyo():
			events.store_event(self.conn_a, late, "Webhook")
			frappe.db.commit()
			events.process_pending(self.conn_a.name)
		self.assertEqual(self.customer(data_id).customer_name, "Ayesha K.")
		self.assertEqual(
			frappe.db.get_value("Koomyo Event Log", {"event_id": late["id"]}, "status"), "Skipped"
		)

	def test_orders_use_the_synced_customer(self):
		data_id = load_fixture("event_customer_created")["data"]["id"]
		self.book(customer_event(1))
		event = order_event(
			seq=5,
			lines=[{"quantity": 1, "unit_price": "100.00", "line_total": "100.00"}],
			payments=[("cash", "115.00")],
			items_subtotal="100.00",
			tax="15.00",
			total="115.00",
			amount_due="115.00",
			amount_paid="115.00",
		)
		event["data"]["customer"] = {"id": data_id}
		self.assertEqual(self.book(event).status, "Done")
		self.assertEqual(self.invoice().customer, self.customer(data_id).name)

	def test_same_customer_id_in_two_connections_is_two_customers(self):
		configure_sales(self.conn_b, "_KTB")
		self.conn_b.reload()
		self.book(customer_event(1))
		self.book(customer_event(1, tenant=TENANT_B), self.conn_b, self.fake_b)
		data_id = load_fixture("event_customer_created")["data"]["id"]
		self.assertNotEqual(self.customer(data_id).name, self.customer(data_id, self.conn_b).name)

	def test_switch_off(self):
		self.conn_a.reload()
		self.conn_a.sync_customers = 0
		self.conn_a.save()
		self.assertEqual(self.book(customer_event(1)).status, "Skipped")
		self.assertEqual(frappe.get_all("Customer", filters={"koomyo_connection": self.conn_a.name}), [])

	def test_replay(self):
		event = customer_event(1)
		self.book(event)
		self.book(copy.deepcopy(event) | {"id": "resent", "seq": 2})
		self.assertEqual(len(frappe.get_all("Customer", filters={"koomyo_connection": self.conn_a.name})), 1)
