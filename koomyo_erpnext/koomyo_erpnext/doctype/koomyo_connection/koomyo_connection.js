// Copyright (c) 2026, Sowaan and contributors
// For license information, please see license.txt

frappe.ui.form.on("Koomyo Connection", {
	refresh(frm) {
		frm.set_query("cost_center", "branch_map", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
		frm.set_query("warehouse", "branch_map", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
		const company_filter = () => ({ filters: { company: frm.doc.company, is_group: 0 } });
		for (const field of [
			"income_account",
			"delivery_fee_account",
			"channel_markup_account",
			"payment_adjustment_account",
			"default_cost_center",
			"cash_over_short_account",
			"drawer_account",
			"cogs_account",
			"stock_adjustment_account",
			"transfer_loss_account",
		]) {
			frm.set_query(field, company_filter);
		}
		frm.set_query("price_list", () => ({ filters: { selling: 1, enabled: 1 } }));
		frm.set_query("account", "payment_map", company_filter);
		frm.set_query("fee_account", "payment_map", company_filter);
		frm.set_query("account_head", "tax_map", company_filter);
		frm.set_query("expense_account", "waste_map", company_filter);

		show_status(frm);
		if (frm.is_new()) return;

		frm.add_custom_button(__("Test Connection"), () => test_connection(frm));
		if (frm.doc.webhook_url) {
			frm.add_custom_button(
				__("Copy Webhook Address"),
				() => frappe.utils.copy_to_clipboard(frm.doc.webhook_url),
				__("Webhook")
			);
		}
		frm.add_custom_button(
			__("Event Log"),
			() => frappe.set_route("List", "Koomyo Event Log", { connection: frm.doc.name }),
			__("View")
		);
		if (frm.doc.enabled) {
			frm.add_custom_button(
				__("Catch Up Now"),
				() =>
					frm
						.call("catch_up_now")
						.then((r) => r.message && frappe.show_alert(r.message)),
				__("Feed")
			);
		}
		if (!frm.doc.dry_run) {
			frm.add_custom_button(
				__("Book Dry-Run Events"),
				() =>
					frm
						.call("book_dry_run_events")
						.then((r) => r.message && frappe.show_alert(r.message)),
				__("Feed")
			);
		}
		if (frm.doc.status === "Needs Re-sync") {
			frm.add_custom_button(
				__("Restart Feed"),
				() =>
					frappe.confirm(
						__(
							"The feed will be read again from today. Events of the missing days will not come from the feed; bring them in with a backfill. Continue?"
						),
						() =>
							frm.call("restart_feed").then((r) => {
								frm.reload_doc();
								r.message && frappe.show_alert(r.message);
							})
					),
				__("Feed")
			);
		}
	},
});

function show_status(frm) {
	const colors = {
		Connected: "green",
		"Not Tested": "orange",
		"Needs Attention": "red",
		"Needs Re-sync": "red",
	};
	if (frm.doc.status && frm.doc.status !== "Connected") {
		frm.dashboard.set_headline_alert(
			`<b>${__(frm.doc.status)}</b>${
				frm.doc.status_message
					? ": " + frappe.utils.escape_html(frm.doc.status_message)
					: ""
			}`,
			colors[frm.doc.status] || "orange"
		);
	}
}

function test_connection(frm) {
	if (frm.is_dirty()) {
		frappe.msgprint(__("Save the connection first."));
		return;
	}
	frm.call({
		method: "test_connection",
		doc: frm.doc,
		freeze: true,
		freeze_message: __("Calling Koomyo..."),
	}).then((r) => {
		frm.reload_doc();
		const res = r.message || {};
		const esc = frappe.utils.escape_html;
		let html = "";
		if (res.restaurant) {
			html += `<p><b>${__("Restaurant")}:</b> ${esc(res.restaurant)} (${esc(
				res.currency
			)}, ${esc(res.timezone)})</p>`;
			html += `<p><b>${__("Key scopes")}:</b> ${
				esc((res.scopes || []).join(", ")) || "-"
			}</p>`;
		}
		if (res.error) html += `<p class="text-danger">${esc(res.error)}</p>`;
		(res.warnings || []).forEach((w) => (html += `<p class="text-warning">${esc(w)}</p>`));
		if ((res.branches || []).length) {
			html += `<table class="table table-bordered table-sm"><tr><th>${__(
				"Branch"
			)}</th><th>${__("Code")}</th><th>${__("Mapped")}</th></tr>`;
			res.branches.forEach((b) => {
				html += `<tr><td>${esc(b.name)}${
					b.active ? "" : " (" + __("inactive") + ")"
				}</td><td>${esc(b.code || "")}</td><td>${
					b.mapped ? __("Yes") : __("No")
				}</td></tr>`;
			});
			html += "</table>";
		}
		const unmapped = (res.branches || []).filter((b) => !b.mapped);
		const d = new frappe.ui.Dialog({
			title: res.ok ? __("Connected") : __("Connection needs attention"),
			fields: [{ fieldtype: "HTML", options: html }],
			primary_action_label: unmapped.length ? __("Add Unmapped Branches") : __("Close"),
			primary_action() {
				d.hide();
				if (!unmapped.length) return;
				unmapped.forEach((b) => {
					const row = frm.add_child("branch_map");
					row.koomyo_branch_id = b.id;
					row.koomyo_branch_name = b.name;
				});
				frm.refresh_field("branch_map");
				frappe.show_alert(
					__("Pick a Cost Center and Warehouse for each new row, then save.")
				);
			},
		});
		d.show();
	});
}
