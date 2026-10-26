// Copyright (c) 2026, Sowaan and contributors
// For license information, please see license.txt

frappe.listview_settings["Koomyo Backfill"] = {
	add_fields: ["status"],
	get_indicator(doc) {
		const colors = {
			Draft: "gray",
			Previewing: "blue",
			Previewed: "blue",
			Queued: "orange",
			Running: "orange",
			Stopping: "orange",
			Stopped: "orange",
			Completed: "green",
			Failed: "red",
		};
		return [__(doc.status), colors[doc.status] || "gray", `status,=,${doc.status}`];
	},
	onload(listview) {
		listview.page.add_inner_button(__("All Connections"), () => {
			const d = new frappe.ui.Dialog({
				title: __("One backfill per connection"),
				fields: [
					{ fieldname: "from_date", fieldtype: "Date", label: __("From Date"), reqd: 1 },
					{ fieldname: "to_date", fieldtype: "Date", label: __("To Date"), reqd: 1 },
					{
						fieldname: "bring_sales",
						fieldtype: "Check",
						label: __("Sales and Refunds"),
						default: 1,
					},
					{ fieldname: "bring_shifts", fieldtype: "Check", label: __("Shifts") },
					{
						fieldname: "bring_stock",
						fieldtype: "Check",
						label: __("Stock Movements and Transfers"),
					},
					{ fieldname: "bring_customers", fieldtype: "Check", label: __("Customers") },
					{ fieldname: "bring_purchases", fieldtype: "Check", label: __("Purchases") },
				],
				primary_action_label: __("Create"),
				primary_action(values) {
					frappe
						.call(
							"koomyo_erpnext.koomyo_erpnext.doctype.koomyo_backfill.koomyo_backfill.create_for_all_connections",
							values
						)
						.then((r) => {
							d.hide();
							const res = r.message || {};
							let msg = __(
								"{0} backfill(s) created. Open each one, Preview, then Start.",
								[(res.made || []).length]
							);
							if ((res.refused || []).length) {
								msg +=
									"<br><br>" +
									__("Not created:") +
									"<br>" +
									res.refused
										.map((x) => frappe.utils.escape_html(x))
										.join("<br>");
							}
							frappe.msgprint(msg);
							listview.refresh();
						});
				},
			});
			d.show();
		});
	},
};
