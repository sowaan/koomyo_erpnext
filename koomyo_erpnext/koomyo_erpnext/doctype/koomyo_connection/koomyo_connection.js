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
		frm.set_query("customer_group", () => ({ filters: { is_group: 0 } }));
		frm.set_query("supplier_group", () => ({ filters: { is_group: 0 } }));
		frm.set_query("main_warehouse", company_filter);
		frm.set_query("territory", () => ({ filters: { is_group: 0 } }));
		frm.set_query("account", "payment_map", company_filter);
		frm.set_query("fee_account", "payment_map", company_filter);
		frm.set_query("account_head", "tax_map", company_filter);
		frm.set_query("expense_account", "waste_map", company_filter);

		show_status(frm);
		render_checklist(frm);
		if (frm.is_new()) return;

		// Two buttons on top; everything else lives in one Actions menu.
		frm.add_custom_button(__("Setup Wizard"), () => {
			frappe.route_options = { connection: frm.doc.name };
			frappe.set_route("koomyo-setup");
		}).addClass("btn-primary");
		frm.add_custom_button(__("Test Connection"), () => test_connection(frm));

		const actions = __("Actions");
		const action = (label, fn) => frm.add_custom_button(label, fn, actions);
		action(__("Set Up From Koomyo"), () => auto_setup(frm));
		if ((frm.doc.setup_log || []).length) {
			action(__("Undo Set Up"), () =>
				frappe.confirm(
					__(
						"Remove the records Set Up From Koomyo created? Records already used by documents are kept."
					),
					() =>
						frm.call("undo_auto_setup").then((r) => {
							const res = r.message || {};
							frappe.msgprint({
								title: __("Undo Set Up"),
								message:
									`<p>${__("Removed")}: ${(res.removed || []).length}</p>` +
									((res.kept || []).length
										? `<p>${__("Kept, because something uses them")}:<br>${(
												res.kept || []
										  )
												.map(frappe.utils.escape_html)
												.join("<br>")}</p>`
										: ""),
							});
							frm.reload_doc();
						})
				)
			);
		}
		action(__("Preview Menu Load"), () => load_catalog(frm, 1));
		action(__("Load Menu and Ingredients"), () =>
			frappe.confirm(
				__(
					"Make an Item for every Koomyo menu item and ingredient now? Existing ones are updated, nothing is doubled."
				),
				() => load_catalog(frm, 0)
			)
		);
		if (frm.doc.enabled) {
			action(__("Catch Up Now"), () =>
				frm.call("catch_up_now").then((r) => r.message && frappe.show_alert(r.message))
			);
		}
		if (!frm.doc.dry_run) {
			action(__("Book Dry-Run Events"), () =>
				frm
					.call("book_dry_run_events")
					.then((r) => r.message && frappe.show_alert(r.message))
			);
		}
		if (frm.doc.status === "Needs Re-sync") {
			action(__("Restart Feed"), () =>
				frappe.confirm(
					__(
						"The feed will be read again from today. Events of the missing days will not come from the feed; bring them in with a backfill. Continue?"
					),
					() =>
						frm.call("restart_feed").then((r) => {
							frm.reload_doc();
							r.message && frappe.show_alert(r.message);
						})
				)
			);
		}
		if (frm.doc.webhook_url) {
			action(__("Copy Webhook Address"), () =>
				frappe.utils.copy_to_clipboard(frm.doc.webhook_url)
			);
		}
		action(__("Event Log"), () =>
			frappe.set_route("List", "Koomyo Event Log", { connection: frm.doc.name })
		);
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

function load_catalog(frm, preview) {
	if (frm.is_dirty()) {
		frappe.msgprint(__("Save the connection first."));
		return;
	}
	frm.call("load_catalog", { preview }).then((r) => {
		r.message && frappe.show_alert(r.message);
		// The load runs in the background: show its result when it lands.
		setTimeout(() => frm.reload_doc(), 8000);
	});
}

function render_checklist(frm) {
	const field = frm.get_field("setup_checklist");
	if (!field) return;
	const todo = (frm.doc.__onload && frm.doc.__onload.checklist) || [];
	const esc = frappe.utils.escape_html;
	if (frm.is_new()) {
		field.$wrapper.html("");
		return;
	}
	field.$wrapper.html(
		todo.length
			? `<div class="alert alert-warning" style="margin-bottom:12px"><b>${__(
					"Still to do"
			  )}</b><ul style="margin:6px 0 0 16px">${todo
					.map(
						(t) =>
							`<li><a href="#" class="koomyo-todo" data-tab="${esc(t.tab)}">${esc(
								t.text
							)}</a></li>`
					)
					.join("")}</ul></div>`
			: `<div class="alert alert-success" style="margin-bottom:12px">${__(
					"Nothing missing: every switched-on sync has what it needs."
			  )}</div>`
	);
	field.$wrapper.find(".koomyo-todo").on("click", (e) => {
		e.preventDefault();
		const tab = frm.layout.tabs.find((t) => t.df.fieldname === $(e.currentTarget).data("tab"));
		tab && tab.set_active();
	});
}

function auto_setup(frm) {
	if (frm.is_dirty()) {
		frappe.msgprint(__("Save the connection first."));
		return;
	}
	frm.call({
		method: "auto_setup_plan",
		doc: frm.doc,
		freeze: true,
		freeze_message: __("Reading Koomyo..."),
	}).then((r) => {
		const plan = r.message || {};
		const rows = plan.rows || [];
		if (!rows.length) {
			frappe.msgprint(
				__("Nothing to set up: every setting Koomyo can fill is already filled.") +
					(plan.notes || []).map((n) => "<br>" + frappe.utils.escape_html(n)).join("")
			);
			return;
		}
		const d = new frappe.ui.Dialog({
			title: __("Set Up From Koomyo: {0}", [plan.restaurant]),
			size: "extra-large",
			fields: [
				{
					fieldtype: "HTML",
					options: `<p class="text-muted">${__(
						"Nothing is saved until you press Apply. Empty settings only: filled ones are never changed. Change a row to use another record or skip it."
					)}${(plan.notes || [])
						.map((n) => "<br><b>" + frappe.utils.escape_html(n) + "</b>")
						.join("")}</p>`,
				},
				{
					fieldname: "rows",
					fieldtype: "Table",
					cannot_add_rows: true,
					cannot_delete_rows: true,
					in_place_edit: true,
					data: rows,
					fields: [
						{
							fieldname: "section",
							fieldtype: "Data",
							label: __("Section"),
							read_only: 1,
							in_list_view: 1,
							columns: 1,
						},
						{
							fieldname: "label",
							fieldtype: "Data",
							label: __("Setting"),
							read_only: 1,
							in_list_view: 1,
							columns: 3,
						},
						{
							fieldname: "action",
							fieldtype: "Select",
							label: __("Action"),
							options: ["Use existing", "Create", "Skip"],
							in_list_view: 1,
							columns: 2,
						},
						{ fieldname: "doctype", fieldtype: "Data", label: __("Type"), hidden: 1 },
						{
							fieldname: "value",
							fieldtype: "Dynamic Link",
							options: "doctype",
							label: __("Record, or name to create"),
							in_list_view: 1,
							columns: 3,
						},
						{
							fieldname: "note",
							fieldtype: "Data",
							label: __("Note"),
							read_only: 1,
							in_list_view: 1,
							columns: 2,
						},
						{ fieldname: "id", fieldtype: "Data", hidden: 1 },
					],
				},
				{ fieldtype: "Section Break" },
				{
					fieldname: "load_menu",
					fieldtype: "Check",
					label: __("Then load the menu and ingredients"),
					default: 0,
				},
			],
			primary_action_label: __("Apply"),
			primary_action(values) {
				const choices = (values.rows || []).map((x) => ({
					id: x.id,
					action: x.action,
					value: x.value,
				}));
				frm.call("auto_setup_apply", {
					choices: JSON.stringify(choices),
					load_menu: values.load_menu ? 1 : 0,
				}).then((res) => {
					d.hide();
					const out = res.message || {};
					const list = (a) => (a || []).map(frappe.utils.escape_html).join("<br>");
					frappe.msgprint({
						title: __("Set Up From Koomyo"),
						message:
							`<p><b>${__("Created")}</b> (${(out.created || []).length}):<br>${list(
								out.created
							)}</p>` +
							((out.used || []).length
								? `<p><b>${__("Already there, used as they were")}</b>:<br>${list(
										out.used
								  )}</p>`
								: "") +
							`<p>${__(
								"Syncs are still off. Review the tabs, then switch on what you want."
							)}</p>` +
							(out.menu ? `<p>${frappe.utils.escape_html(out.menu)}</p>` : ""),
					});
					frm.reload_doc();
				});
			},
		});
		d.show();
	});
}
