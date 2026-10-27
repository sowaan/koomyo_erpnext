"""Section 6: the Koomyo supplier card -> Supplier, Address, Contact and Payment Terms Template.

Samples start from the docs examples (fixtures/event_supplier_created.json, event_supplier_updated.json).
"""

import copy

import frappe

from koomyo_erpnext.tests.fake_koomyo import load_fixture
from koomyo_erpnext.tests.helpers import TENANT_A, hard_delete, purge_orphan_contact_rows
from koomyo_erpnext.tests.test_backfill import BackfillTestCase
from koomyo_erpnext.tests.test_purchase_orders import PurchaseOrderCase, make_order

SUPPLIER_ID = "5bb41bd1-ec94-409b-ae63-08b6b4a97ed2"


def card(**changes):
	data = copy.deepcopy(load_fixture("event_supplier_created")["data"])
	data.update(
		{
			"id": SUPPLIER_ID,
			"name": "_Koomyo Cheese Co",
			"code": "SUP-0001",
			"contact_name": "Ayesha Khan",
			"phone": "+92 300 1234567",
			"alt_phone": "+92 42 111 000",
			"whatsapp": "+92 300 7654321",
			"email": "orders@cheese.example",
			"website": "https://cheese.example",
			"address": "12 Mall Road",
			"address_line2": "Block B",
			"city": "Lahore",
			"region": "Punjab",
			"country": "Pakistan",
			"postal_code": "54000",
			"tax_number": "1234567-8",
			"tax_number2": "STRN 17-00-1234-567-89",
			"tax_status": "registered",
			"payment_terms": "net",
			"payment_days": 30,
			"credit_limit": "250000.00",
			"lead_time_days": 2,
			"min_order_value": "5000.00",
			"delivery_days": ["mon", "thu"],
			"tags": ["Dairy", "Cold chain"],
			"preferred": True,
			"rating": 4,
			"notes": "Call before 10",
			"updated_at": "2026-10-12T16:54:31Z",
			"external_refs": {},
		}
	)
	data.update(changes)
	return data


def supplier_event(data, event_type="supplier.created", seq=300, eid=None):
	event = copy.deepcopy(load_fixture("event_supplier_created"))
	event.update(
		{
			"type": event_type,
			"tenant_id": TENANT_A,
			"seq": seq,
			"id": eid or f"evt-sup-{seq}",
			"data": data,
			"entity": {"type": "supplier", "id": data["id"]},
		}
	)
	return event


class SupplierCase(PurchaseOrderCase):
	def setUp(self):
		purge_orphan_contact_rows()
		super().setUp()

	def tearDown(self):
		for name in frappe.get_all("Supplier", filters={"koomyo_connection": self.conn_a.name}, pluck="name"):
			for doctype in ("Address", "Contact"):
				for link in frappe.get_all(
					"Dynamic Link",
					filters={"parenttype": doctype, "link_doctype": "Supplier", "link_name": name},
					pluck="parent",
				):
					hard_delete(doctype, link)
		super().tearDown()

	def supplier(self, supplier_id=SUPPLIER_ID):
		name = frappe.db.get_value(
			"Supplier", {"koomyo_connection": self.conn_a.name, "koomyo_id": supplier_id}
		)
		return frappe.get_doc("Supplier", name) if name else None

	def send(self, data, event_type="supplier.created", **kw):
		self.po_seq += 1
		return self.book(supplier_event(data, event_type, self.po_seq, **kw))


class TestSupplierCard(SupplierCase):
	def test_the_whole_card_lands_in_supplier_address_and_contact(self):
		log = self.send(card())
		self.assertEqual(log.status, "Done", (log.message, log.error[-600:]))
		s = self.supplier()
		self.assertEqual(
			(s.supplier_name, s.tax_id, s.website, s.disabled),
			("_Koomyo Cheese Co", "1234567-8", "https://cheese.example", 0),
		)
		self.assertEqual((s.koomyo_branch, s.supplier_group), ("origin:koomyo", "_Koomyo Suppliers"))
		for line in (
			"SUP-0001",
			"STRN 17-00-1234-567-89",
			"registered",
			"250000.00",
			"Lead time (days): 2",
			"5000.00",
			"Monday, Thursday",
			"Dairy, Cold chain",
			"Preferred supplier: Yes",
			"Rating (1 to 5): 4",
			"Call before 10",
		):
			self.assertIn(line, s.supplier_details)
		self.assertEqual(s.payment_terms, "Koomyo Net 30")
		template = frappe.get_doc("Payment Terms Template", "Koomyo Net 30")
		self.assertEqual((template.terms[0].credit_days, template.terms[0].invoice_portion), (30, 100))

		address = frappe.get_doc("Address", frappe.db.get_value("Address", {"koomyo_id": SUPPLIER_ID}))
		self.assertEqual(
			(
				address.address_line1,
				address.address_line2,
				address.city,
				address.state,
				address.country,
				address.pincode,
			),
			("12 Mall Road", "Block B", "Lahore", "Punjab", "Pakistan", "54000"),
		)
		self.assertEqual((address.links[0].link_doctype, address.links[0].link_name), ("Supplier", s.name))
		contact = frappe.get_doc("Contact", frappe.db.get_value("Contact", {"koomyo_id": SUPPLIER_ID}))
		self.assertEqual(contact.first_name, "Ayesha Khan")
		self.assertEqual(
			[p.phone for p in contact.phone_nos], ["+92 300 1234567", "+92 42 111 000", "+92 300 7654321"]
		)
		self.assertEqual([e.email_id for e in contact.email_ids], ["orders@cheese.example"])
		self.assertEqual(contact.links[0].link_name, s.name)

	def test_bank_details_are_not_a_thing_here(self):
		self.send(card())
		fields = {df.fieldname for df in frappe.get_meta("Supplier").fields}
		self.assertNotIn("bank_account", {f for f in fields if "bank_account_no" in f})
		self.assertNotIn("bank", self.supplier().supplier_details.lower())

	def test_payment_terms_cod_advance_and_net(self):
		for terms, days, name in (
			("cod", None, "Koomyo Cash on Delivery"),
			("advance", None, "Koomyo Payment in Advance"),
			("net", 45, "Koomyo Net 45"),
		):
			sid = f"sup-{terms}"
			log = self.send(
				card(id=sid, name=f"_K {terms}", payment_terms=terms, payment_days=days, address=None)
			)
			self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
			self.assertEqual(self.supplier(sid).payment_terms, name)
		self.assertEqual(frappe.db.get_value("Payment Term", "Koomyo Cash on Delivery", "credit_days"), 0)

	def test_net_without_days_and_unknown_terms_are_refused(self):
		log = self.send(card(id="s1", name="_K a", payment_days=None))
		self.assertEqual(log.status, "Failed")
		self.assertIn("number of days", log.message)
		self.assertIsNone(self.supplier("s1"))
		log = self.send(card(id="s2", name="_K b", payment_terms="barter"))
		self.assertEqual(log.status, "Failed")

	def test_a_sparse_card_is_enough(self):
		bare = {
			k: None
			for k in card()
			if k
			not in (
				"id",
				"object",
				"name",
				"active",
				"created_at",
				"external_refs",
				"delivery_days",
				"tags",
				"preferred",
			)
		}
		bare.update(
			id="s3",
			object="supplier",
			name="_K bare",
			active=True,
			external_refs={},
			delivery_days=[],
			tags=[],
			preferred=False,
		)
		log = self.send(bare)
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		self.assertIsNone(frappe.db.get_value("Address", {"koomyo_id": "s3"}))
		self.assertIsNone(frappe.db.get_value("Contact", {"koomyo_id": "s3"}))
		self.assertFalse(self.supplier("s3").payment_terms)

	def test_an_address_missing_a_line_city_or_country_is_not_guessed(self):
		log = self.send(card(id="s4", name="_K c", city=None, country="Atlantis"))
		self.assertEqual(log.status, "Done")
		self.assertIsNone(frappe.db.get_value("Address", {"koomyo_id": "s4"}))
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Supplier", "reference_name": self.supplier("s4").name},
			pluck="content",
		)
		self.assertTrue(
			any("address not made" in c and "city" in c and "known country" in c for c in comments), comments
		)

	def test_koomyos_text_is_escaped(self):
		self.send(card(id="s5", name="_K d", notes="<script>alert(1)</script>"))
		self.assertNotIn("<script>", self.supplier("s5").supplier_details)
		self.assertIn("&lt;script&gt;", self.supplier("s5").supplier_details)


class TestSupplierUpdates(SupplierCase):
	def test_an_update_changes_the_card_once_and_a_late_copy_changes_nothing(self):
		self.send(card())
		before = self.supplier().name
		newer = card(
			phone="+92 300 0000000", payment_days=60, active=False, updated_at="2026-10-13T08:00:00Z"
		)
		self.assertEqual(self.send(newer, "supplier.updated").status, "Done")
		s = self.supplier()
		self.assertEqual((s.name, s.disabled, s.payment_terms), (before, 1, "Koomyo Net 60"))
		contact = frappe.get_doc("Contact", frappe.db.get_value("Contact", {"koomyo_id": SUPPLIER_ID}))
		self.assertEqual(contact.phone_nos[0].phone, "+92 300 0000000")
		self.assertEqual(len(frappe.get_all("Contact", filters={"koomyo_id": SUPPLIER_ID})), 1)
		self.assertEqual(len(frappe.get_all("Address", filters={"koomyo_id": SUPPLIER_ID})), 1)

		late = card(phone="+92 111", updated_at="2026-10-12T17:00:00Z")
		self.assertEqual(self.send(late, "supplier.updated").status, "Done")
		self.assertEqual(self.supplier().disabled, 1, "the older copy did not undo it")

	def test_a_rename_in_koomyo_follows_unless_the_name_is_taken(self):
		self.send(card())
		self.send(card(name="_Koomyo Cheese Co Ltd", updated_at="2026-10-13T08:00:00Z"), "supplier.updated")
		self.assertEqual(self.supplier().supplier_name, "_Koomyo Cheese Co Ltd")

	def test_a_supplier_made_in_erpnext_is_never_overwritten_by_its_echo(self):
		mine = self.supplier_in_erpnext("_Koomyo Dairy")
		echo = card(
			id="sup-mine", name="Koomyo's other name", external_refs={"erpnext": mine.name}, tax_number="999"
		)
		log = self.send(echo, "supplier.updated")
		self.assertEqual(log.status, "Skipped")
		self.assertIn("made in ERPNext", log.message)
		self.assertNotEqual(frappe.db.get_value("Supplier", mine.name, "tax_id"), "999")

	def supplier_in_erpnext(self, name):
		doc = frappe.get_doc("Supplier", name)
		doc.db_set({"koomyo_connection": self.conn_a.name, "koomyo_id": "sup-mine", "koomyo_branch": None})
		return doc

	def test_a_name_already_in_erpnext_is_never_merged(self):
		log = self.send(card(id="s6", name="_Koomyo Dairy"))
		self.assertEqual(log.status, "Done")
		self.assertEqual(self.supplier("s6").supplier_name, f"_Koomyo Dairy ({self.conn_a.name})")

	def test_off_when_orders_made_in_koomyo_are_not_booked(self):
		self.conn_a.reload()
		self.conn_a.book_koomyo_purchases = 0
		self.conn_a.save()
		log = self.send(card())
		self.assertEqual(log.status, "Skipped")
		self.assertIn("Book Orders Made in Koomyo", log.message)
		self.assertIsNone(self.supplier())

	def test_the_supplier_group_is_needed(self):
		self.conn_a.reload()
		self.conn_a.supplier_group = None
		self.conn_a.save()
		log = self.send(card())
		self.assertEqual(log.error_class, "NeedsMapping")


class TestSupplierOnAnOrder(SupplierCase):
	def test_a_purchase_order_makes_the_supplier_from_the_whole_card(self):
		self.fake_a.lists["/suppliers"] = [card(id="koomyo-supplier-1", name="Koomyo Cheese Co")]
		log = self.send_order(make_order("ordered"))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		s = self.supplier("koomyo-supplier-1")
		self.assertEqual((s.tax_id, s.payment_terms), ("1234567-8", "Koomyo Net 30"))
		self.assertTrue(frappe.db.get_value("Address", {"koomyo_id": "koomyo-supplier-1"}))

	def test_without_the_card_the_order_still_books_and_the_event_fills_it_in_later(self):
		log = self.send_order(make_order("ordered"))
		self.assertEqual(log.status, "Done", (log.message, log.error[-300:]))
		s = self.supplier("koomyo-supplier-1")
		self.assertEqual((s.supplier_name, s.tax_id), ("Koomyo Cheese Co", None))
		self.assertEqual(self.po().supplier, s.name)
		self.send(card(id="koomyo-supplier-1", name="Koomyo Cheese Co"), "supplier.updated")
		self.assertEqual(self.supplier("koomyo-supplier-1").tax_id, "1234567-8")
		self.assertEqual(self.supplier("koomyo-supplier-1").name, s.name)

	def send_order(self, order):
		from koomyo_erpnext.tests.test_purchase_orders import event_for

		self.po_seq += 1
		return self.book(event_for(order, "purchase.updated", self.po_seq))


class TestSupplierBackfill(BackfillTestCase):
	"""A backfill with Purchases ticked brings in every supplier card, like supplier.updated would."""

	def setUp(self):
		purge_orphan_contact_rows()
		super().setUp()
		if not frappe.db.exists("Supplier Group", "_Koomyo Suppliers"):
			frappe.get_doc(
				{
					"doctype": "Supplier Group",
					"supplier_group_name": "_Koomyo Suppliers",
					"parent_supplier_group": "All Supplier Groups",
				}
			).insert()
		self.conn_a.reload()
		self.conn_a.update(
			{
				"sync_purchasing": 1,
				"book_koomyo_purchases": 1,
				"supplier_group": "_Koomyo Suppliers",
				"main_warehouse": "Stores - _KTA",
			}
		)
		self.conn_a.save()
		self.fake_a.lists["/suppliers"] = [
			card(id="bf-1", name="_BF Dairy", created_at="2026-10-01T08:00:00Z"),
			card(
				id="bf-2",
				name="_BF Bakery",
				created_at="2026-10-05T08:00:00Z",
				payment_terms="cod",
				address=None,
			),
			card(id="bf-late", name="_BF Later", created_at="2026-12-01T08:00:00Z"),
		]
		self.fake_a.lists["/purchase-orders"] = []

	def tearDown(self):
		for name in frappe.get_all("Supplier", filters={"koomyo_connection": self.conn_a.name}, pluck="name"):
			for doctype in ("Address", "Contact"):
				for link in frappe.get_all(
					"Dynamic Link",
					filters={"parenttype": doctype, "link_doctype": "Supplier", "link_name": name},
					pluck="parent",
				):
					hard_delete(doctype, link)
		super().tearDown()

	def suppliers(self):
		return sorted(
			frappe.get_all("Supplier", filters={"koomyo_connection": self.conn_a.name}, pluck="koomyo_id")
		)

	def test_preview_counts_run_brings_them_in_and_a_second_run_changes_nothing(self):
		doc = self.press(self.make(bring_sales=0, bring_purchases=1, to_date="2026-10-13"), "preview")
		self.assertEqual((doc.found, doc.created, doc.failed), (2, 2, 0), doc.error)
		self.assertEqual(self.suppliers(), [], "a preview writes nothing")
		doc = self.press(self.make(bring_sales=0, bring_purchases=1, to_date="2026-10-13"))
		self.assertEqual((doc.status, doc.created, doc.failed), ("Completed", 2, 0), doc.error)
		self.assertEqual(self.suppliers(), ["bf-1", "bf-2"], "a supplier made after the range is left out")
		again = self.press(self.make(bring_sales=0, bring_purchases=1, to_date="2026-10-13"))
		self.assertEqual((again.created, again.already_there), (0, 2))
		self.assertEqual(len(self.suppliers()), 2)
		self.assertTrue(frappe.db.exists("Address", {"koomyo_id": "bf-1"}))

	def test_a_live_event_and_a_backfill_never_double(self):
		live = supplier_event(
			card(id="bf-1", name="_BF Dairy", updated_at="2026-10-12T16:54:31Z"), "supplier.updated"
		)
		self.assertEqual(self.book(live).status, "Done")
		doc = self.press(self.make(bring_sales=0, bring_purchases=1, to_date="2026-10-13"))
		self.assertEqual((doc.created, doc.already_there), (1, 1))

	def test_a_changed_card_is_brought_up_to_date(self):
		self.press(self.make(bring_sales=0, bring_purchases=1, to_date="2026-10-13"))
		self.fake_a.lists["/suppliers"][0] = card(
			id="bf-1",
			name="_BF Dairy",
			created_at="2026-10-01T08:00:00Z",
			phone="+92 111",
			updated_at="2026-10-20T08:00:00Z",
		)
		doc = self.press(self.make(bring_sales=0, bring_purchases=1, to_date="2026-10-13"))
		self.assertEqual((doc.created, doc.already_there), (1, 1))
		contact = frappe.get_doc("Contact", frappe.db.get_value("Contact", {"koomyo_id": "bf-1"}))
		self.assertEqual(contact.phone_nos[0].phone, "+92 111")
