// Copyright (c) 2026, Sowaan and contributors
// For license information, please see license.txt

frappe.ui.form.on("Koomyo Settings", {
	refresh(frm) {
		frm.add_custom_button(__("New Connection"), () => frappe.new_doc("Koomyo Connection"));
		frappe.db
			.get_list("Koomyo Connection", {
				fields: ["name", "company", "restaurant_name", "enabled", "status"],
				limit: 100,
			})
			.then((rows) => {
				const esc = frappe.utils.escape_html;
				let html = rows.length
					? `<table class="table table-bordered"><tr><th>${__("Connection")}</th><th>${__(
							"Company"
					  )}</th><th>${__("Restaurant")}</th><th>${__("Enabled")}</th><th>${__("Status")}</th></tr>`
					: `<p class="text-muted">${__("No connections yet. Add one per Company with New Connection.")}</p>`;
				rows.forEach((r) => {
					html += `<tr><td><a href="/app/koomyo-connection/${encodeURIComponent(r.name)}">${esc(
						r.name
					)}</a></td><td>${esc(r.company)}</td><td>${esc(r.restaurant_name || "")}</td><td>${
						r.enabled ? __("Yes") : __("No")
					}</td><td>${esc(__(r.status))}</td></tr>`;
				});
				if (rows.length) html += "</table>";
				frm.get_field("connections_html").$wrapper.html(html);
			});
	},
});
