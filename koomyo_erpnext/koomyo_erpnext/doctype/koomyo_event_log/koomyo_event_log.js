// Copyright (c) 2026, Sowaan and contributors
// For license information, please see license.txt

frappe.ui.form.on("Koomyo Event Log", {
	refresh(frm) {
		if (["Failed", "Skipped"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Retry"), () =>
				frappe
					.call("koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log.retry", {
						names: [frm.doc.name],
					})
					.then((r) => {
						frappe.show_alert(r.message);
						frm.reload_doc();
					})
			);
		}
		if (frm.doc.reference_doctype && frm.doc.reference_name) {
			frm.add_custom_button(__("Open Document"), () =>
				frappe.set_route("Form", frm.doc.reference_doctype, frm.doc.reference_name)
			);
		}
	},
});
