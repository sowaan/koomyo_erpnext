"""Show the Koomyo Event Log in the Connections panel of every document the app writes.

The Event Log points at a document with a Dynamic Link (reference_doctype + reference_name), which ERPNext's panel
does not know about, so each document type adds the entry here (hooks: override_doctype_dashboards). The panel
counts the log rows of that one document, never of another document that happens to share the name.
"""

from frappe import _

# The documents the app creates from Koomyo data (the doctypes that carry koomyo_id).
DOCTYPES = (
	"Sales Invoice",
	"POS Invoice",
	"Payment Entry",
	"Journal Entry",
	"Customer",
	"Item",
	"Supplier",
	"Purchase Order",
	"Purchase Receipt",
	"Stock Entry",
	"Stock Reconciliation",
	"Material Request",
	"Issue",
	"Landed Cost Voucher",
)


def add_event_log(data, doctype: str):
	data = data or {}
	groups = data.setdefault("transactions", [])
	label = _("Koomyo")
	group = next((g for g in groups if g.get("label") == label), None)
	if group is None:
		groups.append({"label": label, "items": ["Koomyo Event Log"]})
	elif "Koomyo Event Log" not in group.setdefault("items", []):
		group["items"].append("Koomyo Event Log")
	data.setdefault("non_standard_fieldnames", {})["Koomyo Event Log"] = "reference_name"
	data.setdefault("dynamic_links", {})["reference_name"] = [doctype, "reference_doctype"]
	return data


def _for(doctype: str):
	def dashboard(data=None):
		return add_event_log(data, doctype)

	dashboard.__name__ = doctype.lower().replace(" ", "_")
	return dashboard


# koomyo_erpnext.dashboards.sales_invoice, .purchase_order, ... (one function per doctype, as the hook needs)
for _doctype in DOCTYPES:
	globals()[_doctype.lower().replace(" ", "_")] = _for(_doctype)
