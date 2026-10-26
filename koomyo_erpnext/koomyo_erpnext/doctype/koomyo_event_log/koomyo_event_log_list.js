// Copyright (c) 2026, Sowaan and contributors
// For license information, please see license.txt

const KOOMYO_RETRY = "koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log";

frappe.listview_settings["Koomyo Event Log"] = {
	add_fields: ["status", "error_class"],
	get_indicator(doc) {
		const colors = {
			Pending: "orange",
			Processing: "blue",
			Done: "green",
			Skipped: "gray",
			Failed: "red",
		};
		const label =
			doc.status === "Failed" && doc.error_class
				? `${__("Failed")}: ${doc.error_class}`
				: __(doc.status);
		return [label, colors[doc.status] || "gray", `status,=,${doc.status}`];
	},
	onload(listview) {
		listview.page.add_inner_button(__("Retry All Failed"), () => {
			const connection = (listview.filters.find((f) => f[1] === "connection") || [])[3];
			frappe.confirm(
				connection
					? __("Queue every failed event of {0} again?", [connection])
					: __("Queue every failed event you can see again?"),
				() =>
					frappe.call(`${KOOMYO_RETRY}.retry_all_failed`, { connection }).then((r) => {
						frappe.show_alert(r.message);
						listview.refresh();
					})
			);
		});
		listview.page.add_actions_menu_item(__("Retry"), () => {
			const names = listview.get_checked_items(true);
			if (!names.length) return;
			frappe.call(`${KOOMYO_RETRY}.retry`, { names }).then((r) => {
				frappe.show_alert(r.message);
				listview.refresh();
			});
		});
	},
};
