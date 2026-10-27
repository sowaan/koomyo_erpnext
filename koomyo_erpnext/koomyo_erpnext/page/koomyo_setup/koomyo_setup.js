// Copyright (c) 2026, Sowaan and contributors
// For license information, please see license.txt

// Guided setup, modelled on the Pakistan Compliance wizard: a step rail, one step at a time,
// action steps that must run before Next, and a finish summary.

frappe.pages["koomyo-setup"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Koomyo Setup"),
		single_column: true,
	});
	inject_styles();
	document.body.classList.add("ky-wizard-fs");
	frappe._ky_wizard = new KoomyoSetupWizard(page);
	frappe._ky_wizard.init();
};

frappe.pages["koomyo-setup"].on_page_show = function () {
	document.body.classList.add("ky-wizard-fs");
};
if (!frappe._ky_wizard_router_hook) {
	frappe._ky_wizard_router_hook = true;
	frappe.router.on("change", () => {
		if ((frappe.get_route() || [])[0] !== "koomyo-setup") {
			document.body.classList.remove("ky-wizard-fs");
		}
	});
}

const M = "koomyo_erpnext.setup_wizard";
const ORANGE = "#ff4b00";

const PATHS = {
	check: '<path d="M20 6 9 17l-5-5"/>',
	left: '<path d="m15 18-6-6 6-6"/>',
	right: '<path d="m9 18 6-6-6-6"/>',
	plus: '<path d="M5 12h14M12 5v14"/>',
	info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
	warn: '<path d="m21.7 18-8-14a2 2 0 0 0-3.4 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3Z"/><path d="M12 9v4M12 17h.01"/>',
	sparkles:
		'<path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/>',
	plug: '<path d="M12 22v-5M9 8V2M15 8V2M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/>',
	wand: '<path d="m15 4 5 5L9 20l-5-5Z"/><path d="m14 5 5 5"/>',
	sync: '<path d="M21 12a9 9 0 0 0-15-6.7L3 8M3 3v5h5M3 12a9 9 0 0 0 15 6.7L21 16M21 21v-5h-5"/>',
	menu: '<path d="M4 6h16M4 12h16M4 18h10"/>',
	globe: '<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 0 1 0 20 15 15 0 0 1 0-20Z"/>',
};
function ic(name, size = 16) {
	return `<svg class="ky-ic" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${
		PATHS[name] || ""
	}</svg>`;
}

const STEPS = [
	{ key: "welcome", title: __("Welcome"), sub: __("Company and name"), icon: "sparkles" },
	{ key: "connect", title: __("Connect"), sub: __("API key and test"), icon: "plug" },
	{ key: "setup", title: __("Set Up"), sub: __("Maps and accounts"), icon: "wand" },
	{ key: "syncs", title: __("What to Sync"), sub: __("Sales, stock, more"), icon: "sync" },
	{ key: "menu", title: __("Menu"), sub: __("Items and ingredients"), icon: "menu" },
	{ key: "webhook", title: __("Webhook"), sub: __("Real-time orders"), icon: "globe" },
	{ key: "finish", title: __("Finish"), sub: __("Switch it on"), icon: "check" },
];

class KoomyoSetupWizard {
	constructor(page) {
		this.page = page;
		this.i = 0;
		this.done = {};
		this.controls = {};
		this.state = {};
		this.plan = null;
	}

	async init() {
		this.$body = $(this.page.main).addClass("ky-wiz-host");
		this.$body.html(`<div class="ky-wiz-loading">${__("Loading…")}</div>`);
		const wanted = (frappe.route_options || {}).connection;
		frappe.route_options = null;
		this.state = await this.call("get_state", wanted ? { connection: wanted } : {});
		this.conn = this.state.connection;
		if (this.conn) this.after_state();
		this.render_shell();
		this.render_step();
	}

	call(method, args, freeze_message) {
		return frappe
			.call({ method: `${M}.${method}`, args, freeze: !!freeze_message, freeze_message })
			.then((r) => r.message);
	}

	after_state() {
		const c = this.conn;
		this.done.connect = c.status === "Connected";
		this.done.setup = c.maps.branches > 0 && c.maps.payments > 0;
	}

	render_shell() {
		const rail = STEPS.map(
			(s, idx) => `
			<div class="ky-step" data-idx="${idx}">
				<div class="ky-step-dot"><span class="ky-num">${idx + 1}</span><span class="ky-check">${ic(
				"check",
				15
			)}</span></div>
				<div class="ky-step-meta"><div class="ky-step-title">${s.title}</div><div class="ky-step-sub">${
				s.sub
			}</div></div>
			</div>`
		).join('<div class="ky-step-line"></div>');
		this.$body.html(`
			<div class="ky-wiz">
				<aside class="ky-rail">
					<div class="ky-brand">
						<img class="ky-brand-logo" src="/assets/koomyo_erpnext/images/koomyo-icon.png"/>
						<div><div class="ky-brand-title">Koomyo</div><div class="ky-brand-sub">${__(
							"Guided setup"
						)}</div></div>
					</div>
					<div class="ky-steps">${rail}</div>
				</aside>
				<section class="ky-panel">
					<div class="ky-progress"><div class="ky-progress-bar"></div></div>
					<div class="ky-content"></div>
					<footer class="ky-foot">
						<button class="btn ky-btn-ghost ky-back">${ic("left", 15)} ${__("Back")}</button>
						<div class="ky-count"></div>
						<button class="btn ky-btn-primary ky-next">${__("Next")} ${ic("right", 15)}</button>
					</footer>
				</section>
			</div>`);
		this.$content = this.$body.find(".ky-content");
		this.$body.find(".ky-back").on("click", () => this.go(this.i - 1));
		this.$body.find(".ky-next").on("click", () => this.next());
		this.$body.find(".ky-step").on("click", (e) => {
			const idx = +$(e.currentTarget).data("idx");
			if (idx < this.i) this.go(idx);
		});
	}

	update_rail() {
		this.$body.find(".ky-step").each((idx, el) => {
			const key = STEPS[idx].key;
			$(el).toggleClass("active", idx === this.i);
			$(el).toggleClass("done", Boolean(idx < this.i || (idx !== this.i && this.done[key])));
		});
		this.$body
			.find(".ky-progress-bar")
			.css("width", `${(this.i / (STEPS.length - 1)) * 100}%`);
		this.$body.find(".ky-count").text(__("Step {0} of {1}", [this.i + 1, STEPS.length]));
		const last = this.i === STEPS.length - 1;
		this.$body.find(".ky-back").prop("disabled", this.i === 0);
		this.$body
			.find(".ky-next")
			.html(last ? __("Open the Connection") : `${__("Next")} ${ic("right", 15)}`);
	}

	go(idx) {
		if (idx < 0 || idx >= STEPS.length) return;
		this.i = idx;
		this.render_step();
	}

	async next() {
		const step = STEPS[this.i];
		const ok = await this.save_step(step.key);
		if (ok === false) return;
		if (this.i === STEPS.length - 1) {
			frappe.set_route("Form", "Koomyo Connection", this.conn.name);
			return;
		}
		this.done[step.key] = true;
		this.go(this.i + 1);
	}

	render_step() {
		this.controls = {};
		this.$content.removeClass("ky-in").empty();
		const $c = $(`<div class="ky-step-body"></div>`).appendTo(this.$content);
		this[`step_${STEPS[this.i].key}`]($c);
		this.update_rail();
		requestAnimationFrame(() => this.$content.addClass("ky-in"));
	}

	control(parent, df, value) {
		const $wrap = $('<div class="ky-field"></div>').appendTo(parent);
		const c = frappe.ui.form.make_control({ df, parent: $wrap.get(0), render_input: true });
		if (value !== undefined && value !== null) c.set_value(value);
		c.refresh();
		this.controls[df.fieldname] = c;
		return c;
	}

	val(name) {
		return this.controls[name] && this.controls[name].get_value();
	}

	head($c, icon, title, desc) {
		$c.append(`<div class="ky-head"><div class="ky-head-icon">${ic(icon, 22)}</div>
			<div><h2 class="ky-h">${title}</h2><p class="ky-desc">${desc}</p></div></div>`);
	}

	note($c, text, muted) {
		$c.append(
			`<div class="ky-note ${muted ? "ky-note-muted" : ""}">${ic(
				"info",
				15
			)}<span>${text}</span></div>`
		);
	}

	// ------------------------------------------------------------------ steps

	step_welcome($c) {
		this.head(
			$c,
			"sparkles",
			__("Welcome"),
			__(
				"This wizard connects a Koomyo restaurant to a Company: it saves the API key, tests it, fills the maps and accounts from Koomyo, and lets you choose what to sync. One step at a time."
			)
		);
		const existing = (this.state.connections || []).map((c) => c.name);
		if (existing.length) {
			this.control(
				$c,
				{
					fieldtype: "Select",
					fieldname: "existing",
					label: __("Continue an existing connection, or start a new one"),
					options: ["", ...existing].join("\n"),
				},
				this.conn ? this.conn.name : ""
			);
		}
		this.control(
			$c,
			{
				fieldtype: "Link",
				options: "Company",
				fieldname: "company",
				label: __("Company"),
				reqd: 1,
			},
			(this.conn && this.conn.company) || this.state.default_company
		);
		this.control(
			$c,
			{
				fieldtype: "Data",
				fieldname: "connection_name",
				label: __("Connection name"),
				description: __("Such as the restaurant brand."),
			},
			this.conn ? this.conn.name : ""
		);
		this.note(
			$c,
			__(
				"Safe to re-run: filled settings are never overwritten, and nothing is switched on until the last step."
			)
		);
	}

	step_connect($c) {
		const c = this.conn || {};
		this.head(
			$c,
			"plug",
			__("Connect"),
			__(
				"Paste the API key from Koomyo (Settings > Integrations > API keys). It is stored encrypted and never shown again."
			)
		);
		this.control(
			$c,
			{ fieldtype: "Data", fieldname: "base_url", label: __("API address"), reqd: 1 },
			c.base_url || this.state.default_base_url
		);
		this.control($c, {
			fieldtype: "Password",
			fieldname: "api_key",
			label: __("API key"),
			description: c.has_key ? __("A key is already saved. Leave blank to keep it.") : "",
		});
		const g = $('<div class="ky-grid"></div>').appendTo($c);
		this.control(
			g,
			{
				fieldtype: "Date",
				fieldname: "start_from_date",
				label: __("Start from date"),
				description: __(
					"Events before this day are not read. Older data comes in with a backfill."
				),
				reqd: 1,
			},
			c.start_from_date || this.state.today
		);
		this.control(
			g,
			{
				fieldtype: "Select",
				fieldname: "access_mode",
				label: __("Koomyo access"),
				options: "Read and Write\nRead Only",
				description: __("Read Only never writes anything to Koomyo."),
			},
			c.access_mode || "Read and Write"
		);
		const $box = $('<div class="ky-action"></div>').appendTo($c);
		const show = () => {
			const s = this.conn;
			if (!s || s.status !== "Connected") {
				const msg =
					s && s.status === "Needs Attention"
						? frappe.utils.escape_html(s.status_message || "")
						: "";
				$box.html(`<button class="btn ky-btn-primary ky-run">${ic("plug", 15)} ${__(
					"Save and Test Connection"
				)}</button>
					${msg ? `<div class="ky-warn">${ic("warn", 15)}<span>${msg}</span></div>` : ""}
					<div class="ky-run-hint">${__("Required to continue.")}</div>`);
			} else {
				$box.html(`<div class="ky-result"><div class="ky-result-icon">${ic(
					"check",
					20
				)}</div><div>
					<div class="ky-result-title">${__("Connected to {0}", [
						frappe.utils.escape_html(s.restaurant),
					])}</div>
					<ul class="ky-result-list"><li>${frappe.utils.escape_html(
						s.currency
					)} · ${frappe.utils.escape_html(s.timezone)}</li>
					<li>${__("Key scopes")}: ${frappe.utils.escape_html(s.scopes.join(", ") || "-")}</li>
					${
						s.missing_scopes
							? `<li class="ky-bad">${__("Missing")}: ${frappe.utils.escape_html(
									s.missing_scopes
							  )}</li>`
							: ""
					}</ul></div></div>
					<button class="btn ky-btn-soft ky-run">${__("Test again")}</button>`);
			}
			$box.find(".ky-run").on("click", run);
		};
		const run = async () => {
			try {
				await this.save_connection();
				const r = await this.call(
					"test_connection",
					{ connection: this.conn.name },
					__("Calling Koomyo…")
				);
				this.conn = r.state;
				this.done.connect = r.state.status === "Connected";
				if (r.result && r.result.error)
					frappe.show_alert({ message: r.result.error, indicator: "red" }, 8);
			} catch (e) {
				/* frappe shows the error */
			}
			show();
			this.update_rail();
		};
		show();
	}

	async save_connection() {
		const s = await this.call("save_connection", {
			company: this.company,
			connection_name: this.name,
			base_url: this.val("base_url"),
			start_from_date: this.val("start_from_date"),
			access_mode: this.val("access_mode"),
			api_key: this.val("api_key") || undefined,
		});
		this.conn = s;
		return s;
	}

	async step_setup($c) {
		this.head(
			$c,
			"wand",
			__("Set Up From Koomyo"),
			__(
				"Koomyo's branches, payment methods and units are matched to your ERPNext records. Review each line, change it if you like, then apply. Nothing is saved until you press Apply."
			)
		);
		const $box = $('<div class="ky-action"></div>').appendTo($c);
		$box.html(`<div class="ky-run-hint">${__("Reading Koomyo…")}</div>`);
		try {
			this.plan = await this.call(
				"plan",
				{ connection: this.conn.name },
				__("Reading Koomyo…")
			);
		} catch (e) {
			$box.html(
				`<div class="ky-warn">${ic("warn", 15)}<span>${__(
					"Could not read Koomyo. Go back and test the connection."
				)}</span></div>`
			);
			return;
		}
		const rows = this.plan.rows || [];
		if (!rows.length) {
			$box.html(`<div class="ky-result"><div class="ky-result-icon">${ic(
				"check",
				20
			)}</div><div>
				<div class="ky-result-title">${__(
					"Everything Koomyo can fill is already filled."
				)}</div></div></div>`);
			this.done.setup = true;
			this.update_rail();
			return;
		}
		(this.plan.notes || []).forEach((n) => this.note($box, frappe.utils.escape_html(n)));
		const esc = frappe.utils.escape_html;
		let html = `<div class="ky-plan"><table class="table table-sm"><thead><tr><th>${__(
			"Setting"
		)}</th><th>${__("Action")}</th><th>${__(
			"Record, or name to create"
		)}</th><th></th></tr></thead><tbody>`;
		let section = "";
		rows.forEach((r, idx) => {
			if (r.section !== section) {
				section = r.section;
				html += `<tr class="ky-plan-section"><td colspan="4">${esc(section)}</td></tr>`;
			}
			html += `<tr data-idx="${idx}"><td>${esc(r.label)}</td>
				<td><select class="form-control input-sm ky-act">${["Use existing", "Create", "Skip"]
					.map((a) => `<option ${a === r.action ? "selected" : ""}>${a}</option>`)
					.join("")}</select></td>
				<td><input class="form-control input-sm ky-val" value="${esc(r.value || "")}"></td>
				<td class="text-muted small">${esc(r.note || "")}</td></tr>`;
		});
		html += "</tbody></table></div>";
		$box.html(
			html +
				`<label class="ky-check-row"><input type="checkbox" class="ky-load"> ${__(
					"Then load the menu and ingredients"
				)}</label>
			<button class="btn ky-btn-primary ky-apply">${ic("check", 15)} ${__("Apply")}</button>`
		);
		$box.find(".ky-apply").on("click", async () => {
			const choices = rows.map((r, idx) => {
				const $tr = $box.find(`tr[data-idx=${idx}]`);
				return {
					id: r.id,
					action: $tr.find(".ky-act").val(),
					value: $tr.find(".ky-val").val(),
				};
			});
			try {
				const res = await this.call(
					"apply",
					{
						connection: this.conn.name,
						choices: JSON.stringify(choices),
						load_menu: $box.find(".ky-load").is(":checked") ? 1 : 0,
					},
					__("Setting up…")
				);
				this.conn = res.state;
				this.done.setup = true;
				const out = res.result || {};
				$box.html(`<div class="ky-result"><div class="ky-result-icon">${ic(
					"check",
					20
				)}</div><div>
					<div class="ky-result-title">${__("Set up from Koomyo")}</div>
					<ul class="ky-result-list"><li>${__("Created")}: ${(out.created || []).length}</li>
					<li>${__("Already there, used as they were")}: ${(out.used || []).length}</li>
					<li>${__("Branches")}: ${res.state.maps.branches}, ${__("payment methods")}: ${
					res.state.maps.payments
				}, ${__("units")}: ${res.state.maps.units}</li>
					${out.menu ? `<li>${esc(out.menu)}</li>` : ""}</ul></div></div>
					<div class="ky-note ky-note-muted">${ic("info", 15)}<span>${__(
					"Undo Set Up on the connection removes what was created, if nothing uses it yet."
				)}</span></div>`);
				this.update_rail();
			} catch (e) {
				/* frappe shows the error */
			}
		});
	}

	step_syncs($c) {
		const s = (this.conn && this.conn.syncs) || {};
		this.head(
			$c,
			"sync",
			__("What to Sync"),
			__(
				"Switch on what you want. Anything you leave off can be switched on later. Orders and stock rehearse in Dry Run until the last step."
			)
		);
		const sw = (name, label, help, on) => {
			const $sw = $(`<label class="ky-switch"><input type="checkbox" data-name="${name}" ${
				on ? "checked" : ""
			}/>
				<span class="ky-switch-track"><span class="ky-switch-thumb"></span></span>
				<span class="ky-switch-label"><b>${label}</b><small>${help}</small></span></label>`).appendTo($c);
			return $sw;
		};
		sw(
			"sync_sales",
			__("Sales"),
			__("Paid orders become Sales Invoices with their payments."),
			s.sync_sales
		);
		sw("sync_refunds", __("Refunds"), __("Refunds become Credit Notes."), s.sync_refunds);
		sw(
			"sync_shifts",
			__("Shifts and cash"),
			__("The cash over or short of a closed shift becomes a Journal Entry."),
			s.sync_shifts
		);
		sw(
			"sync_stock",
			__("Stock and transfers"),
			__(
				"Ingredient usage, waste, adjustments, counts and transfers become stock documents."
			),
			s.sync_stock
		);
		sw(
			"sync_purchasing",
			__("Purchasing"),
			__(
				"Purchase Orders and receipts made in ERPNext go to Koomyo (not in Read Only), and received orders come back."
			),
			s.sync_purchasing
		);
		sw(
			"book_koomyo_purchases",
			__("Orders made in Koomyo"),
			__("Book supplier orders made in the Koomyo admin as Purchase Orders and receipts."),
			s.book_koomyo_purchases
		);
		sw(
			"sync_customers",
			__("Customers"),
			__("Koomyo customers become Customers (name, phone and email only)."),
			s.sync_customers
		);
		this.control(
			$c,
			{
				fieldtype: "Select",
				fieldname: "stock_deduction",
				label: __("Stock deduction"),
				options: "Daily (from Koomyo usage)\nPer Order (from Koomyo movements)",
				description: __(
					"Per Order takes ingredients out with each sale; Daily books one entry per day."
				),
			},
			s.stock_deduction || "Daily (from Koomyo usage)"
		);
		this.control(
			$c,
			{
				fieldtype: "Select",
				fieldname: "partial_refunds",
				label: __("Partial refunds"),
				options: "Leave for Manual Booking\nProportional Credit Note",
			},
			s.partial_refunds || "Leave for Manual Booking"
		);
		this.$sync_box = $c;
		this.note(
			$c,
			__(
				"If a switched-on sync still needs a setting, the next click lists it. The Set Up step fills most of them."
			),
			true
		);
	}

	step_menu($c) {
		this.head(
			$c,
			"menu",
			__("Menu and Ingredients"),
			__(
				"Optional. Make an Item for every Koomyo menu item (with sizes as variants) and ingredient now, instead of on first sale. You can also do this later from the connection."
			)
		);
		const c = this.conn || {};
		const $box = $('<div class="ky-action"></div>').appendTo($c);
		const show = () => {
			$box.html(`${
				c.catalog_summary
					? `<div class="ky-result"><div class="ky-result-icon">${ic(
							"check",
							20
					  )}</div><div><div class="ky-result-title">${__(
							"Last load"
					  )}</div><ul class="ky-result-list"><li>${frappe.utils
							.escape_html(c.catalog_summary)
							.replace(/\n/g, "<br>")}</li></ul></div></div>`
					: ""
			}
				<button class="btn ky-btn-soft ky-prev">${__("Preview")}</button>
				<button class="btn ky-btn-primary ky-load">${ic("plus", 15)} ${__(
				"Load Menu and Ingredients"
			)}</button>
				<div class="ky-run-hint">${__(
					"Runs in the background. The result shows here and on the connection."
				)}</div>`);
			const go = async (preview) => {
				try {
					await this.call("load_menu", { connection: this.conn.name, preview });
					frappe.show_alert({
						message: __("Loading in the background…"),
						indicator: "blue",
					});
					setTimeout(async () => {
						const st = await this.call("get_state", { connection: this.conn.name });
						this.conn = st.connection;
						c.catalog_summary = this.conn.catalog_summary;
						show();
					}, 9000);
				} catch (e) {
					/* frappe shows the error */
				}
			};
			$box.find(".ky-prev").on("click", () => go(1));
			$box.find(".ky-load").on("click", () => go(0));
		};
		show();
		this.note($c, __("Not now? Click Next to skip."), true);
	}

	step_webhook($c) {
		const c = this.conn || {};
		this.head(
			$c,
			"globe",
			__("Webhook"),
			__(
				"Optional but recommended: Koomyo sends each order the moment it happens. Without it, orders still arrive within about 5 minutes through the events feed."
			)
		);
		$c.append(`<div class="ky-copy"><label>${__(
			"Webhook address"
		)}</label><div class="ky-copy-row"><input class="form-control" readonly value="${frappe.utils.escape_html(
			c.webhook_url || ""
		)}"><button class="btn ky-btn-soft ky-copy-btn">${__("Copy")}</button></div></div>
			<div class="ky-copy"><label>${__(
				"Events to register"
			)}</label><div class="ky-copy-text">${frappe.utils.escape_html(
			c.events_to_register || ""
		)}</div></div>`);
		$c.find(".ky-copy-btn").on("click", () => frappe.utils.copy_to_clipboard(c.webhook_url));
		this.control($c, {
			fieldtype: "Password",
			fieldname: "webhook_secret",
			label: __("Webhook secret"),
			description: c.has_secret
				? __("A secret is already saved. Leave blank to keep it.")
				: __("The whsec_ secret Koomyo shows once when you add the endpoint."),
		});
		this.note(
			$c,
			__(
				"In Koomyo: Settings > Integrations > Webhooks > Add endpoint. The address must be public https (on a laptop, use a tunnel such as ngrok). Keep customer data off unless you sync customers."
			)
		);
	}

	step_finish($c) {
		const c = this.conn || {};
		const on = Object.entries(c.syncs || {})
			.filter(([k, v]) => k.startsWith("sync_") || k === "book_koomyo_purchases")
			.filter(([, v]) => v).length;
		const rows = [
			[__("Connection"), c.name],
			[__("Company"), c.company],
			[__("Restaurant"), c.restaurant],
			[__("Koomyo access"), c.access_mode],
			[
				__("Maps"),
				`${c.maps.branches} ${__("branches")}, ${c.maps.payments} ${__(
					"payment methods"
				)}, ${c.maps.taxes} ${__("tax rows")}, ${c.maps.discounts} ${__(
					"discount sources"
				)}`,
			],
			[__("Syncs switched on"), String(on)],
			[
				__("Webhook secret"),
				c.has_secret ? __("Saved") : __("Not set (the feed still works)"),
			],
		];
		const todo = (c.checklist || [])
			.map((t) => `<li>${frappe.utils.escape_html(t.text)}</li>`)
			.join("");
		$c.append(`<div class="ky-finish"><div class="ky-finish-badge">${ic("check", 34)}</div>
			<h2 class="ky-h">${__("Almost there")}</h2>
			<p class="ky-desc">${__(
				"Choose how to start. You can change this any time on the connection."
			)}</p>
			<div class="ky-summary">${rows
				.map(
					(r) =>
						`<div class="ky-summary-row"><span>${
							r[0]
						}</span><b>${frappe.utils.escape_html(String(r[1] || "—"))}</b></div>`
				)
				.join("")}</div>
			${
				todo
					? `<div class="ky-warn" style="text-align:left">${ic("warn", 15)}<span><b>${__(
							"Still to do"
					  )}</b><ul style="margin:4px 0 0 16px">${todo}</ul></span></div>`
					: ""
			}</div>`);
		this.control(
			$c,
			{
				fieldtype: "Select",
				fieldname: "go_live",
				label: __("Start"),
				options:
					"Dry Run: check everything, save nothing (recommended)\nLive: book real documents now",
			},
			"Dry Run: check everything, save nothing (recommended)"
		);
	}

	// ------------------------------------------------------------------ saving

	async save_step(key) {
		if (key === "welcome") {
			this.company = this.val("company");
			this.name = (this.val("connection_name") || "").trim();
			const existing = this.val("existing");
			if (existing && existing !== (this.conn && this.conn.name)) {
				const st = await this.call("get_state", { connection: existing });
				this.conn = st.connection;
				this.after_state();
				this.company = this.conn.company;
				this.name = this.conn.name;
			}
			if (!this.company || !this.name) {
				frappe.msgprint(__("Choose a Company and give the connection a name."));
				return false;
			}
			return true;
		}
		if (key === "connect") {
			if (!this.done.connect) {
				frappe.msgprint({
					title: __("One step to go"),
					message: __("Press Save and Test Connection, and wait for it to connect."),
					indicator: "orange",
				});
				return false;
			}
			return true;
		}
		if (key === "setup") {
			if (!this.done.setup) {
				frappe.msgprint({
					title: __("One step to go"),
					message: __(
						"Press Apply to fill the maps, or use Back to revisit an earlier step."
					),
					indicator: "orange",
				});
				return false;
			}
			return true;
		}
		if (key === "syncs") {
			const values = {
				stock_deduction: this.val("stock_deduction"),
				partial_refunds: this.val("partial_refunds"),
			};
			this.$sync_box
				.find("input[data-name]")
				.each((i, el) => (values[$(el).data("name")] = $(el).is(":checked") ? 1 : 0));
			try {
				this.conn = await this.call("save_syncs", {
					connection: this.conn.name,
					values: JSON.stringify(values),
				});
			} catch (e) {
				return false;
			}
			const needs = (this.conn.checklist || []).filter((t) => t.tab !== "tab_webhook");
			if (needs.length) {
				frappe.msgprint({
					title: __("Still needed for what you switched on"),
					message:
						`<ul>${needs
							.map((t) => `<li>${frappe.utils.escape_html(t.text)}</li>`)
							.join("")}</ul>` +
						__(
							"You can carry on and fill these in on the connection, or go Back and run Set Up again."
						),
					indicator: "orange",
				});
			}
			return true;
		}
		if (key === "webhook") {
			const secret = this.val("webhook_secret");
			if (secret)
				this.conn = await this.call("save_webhook_secret", {
					connection: this.conn.name,
					secret,
				});
			return true;
		}
		if (key === "finish") {
			const go_live = (this.val("go_live") || "").startsWith("Live") ? 1 : 0;
			try {
				this.conn = await this.call("finish", { connection: this.conn.name, go_live });
			} catch (e) {
				return false;
			}
			return true;
		}
		return true;
	}
}

function inject_styles() {
	if (document.getElementById("ky-wiz-styles")) return;
	const css = `
	body.ky-wizard-fs .body-sidebar-container { display: none !important; }
	.ky-wiz-host { background: var(--gray-100, #f4f5f6); padding: 24px 16px; min-height: calc(100vh - 120px); }
	.ky-wiz-loading { text-align:center; color: var(--text-muted); padding: 80px 0; }
	.ky-ic { display:inline-block; vertical-align:middle; }
	.ky-wiz { display:flex; max-width: 1100px; margin: 0 auto; background:#fff; border-radius:16px; overflow:hidden; box-shadow:0 8px 30px rgba(0,0,0,.08); min-height: 560px; }
	.ky-rail { width: 270px; flex: 0 0 270px; background: #1c1c1e; color:#fff; padding: 26px 22px; display:flex; flex-direction:column; }
	.ky-brand { display:flex; align-items:center; gap:12px; margin-bottom: 28px; }
	.ky-brand-logo { width:40px; height:40px; border-radius:10px; }
	.ky-brand-title { font-weight:700; font-size:16px; }
	.ky-brand-sub { font-size:12px; opacity:.65; }
	.ky-step { display:flex; align-items:center; gap:12px; cursor:default; padding: 4px 0; opacity:.55; }
	.ky-step.active, .ky-step.done { opacity:1; }
	.ky-step.done { cursor:pointer; }
	.ky-step-dot { width:30px; height:30px; flex:0 0 30px; border-radius:50%; border:2px solid rgba(255,255,255,.35); display:flex; align-items:center; justify-content:center; font-size:13px; font-weight:600; }
	.ky-step.active .ky-step-dot { background:${ORANGE}; border-color:${ORANGE}; }
	.ky-step.done .ky-step-dot { background:#2da44e; border-color:#2da44e; }
	.ky-step .ky-check { display:none; } .ky-step.done .ky-check { display:inline-flex; } .ky-step.done .ky-num { display:none; }
	.ky-step-title { font-size:13.5px; font-weight:600; } .ky-step-sub { font-size:11.5px; opacity:.7; }
	.ky-step-line { width:2px; height:14px; background:rgba(255,255,255,.18); margin: 2px 0 2px 14px; }
	.ky-panel { flex:1; display:flex; flex-direction:column; min-width:0; }
	.ky-progress { height:4px; background:#eee; } .ky-progress-bar { height:100%; background:${ORANGE}; transition: width .3s; width:0; }
	.ky-content { flex:1; padding: 30px 38px; opacity:0; transform: translateY(6px); transition: all .25s; overflow:auto; }
	.ky-content.ky-in { opacity:1; transform:none; }
	.ky-head { display:flex; gap:14px; align-items:flex-start; margin-bottom: 20px; }
	.ky-head-icon { width:44px; height:44px; flex:0 0 44px; border-radius:12px; background:#fff1ea; color:${ORANGE}; display:flex; align-items:center; justify-content:center; }
	.ky-h { margin:0 0 4px; font-size:21px; } .ky-desc { margin:0; color: var(--text-muted); max-width: 640px; }
	.ky-field { margin-bottom: 12px; max-width: 640px; }
	.ky-grid { display:grid; grid-template-columns: 1fr 1fr; gap: 0 16px; max-width: 640px; }
	.ky-note, .ky-warn { display:flex; gap:10px; align-items:flex-start; background:#f4f7fb; border-radius:10px; padding:11px 14px; margin-top:14px; font-size:13px; max-width: 700px; }
	.ky-note-muted { background:#f6f6f6; color: var(--text-muted); }
	.ky-warn { background:#fff7e6; color:#8a5a00; }
	.ky-action { margin-top: 14px; }
	.ky-result { display:flex; gap:12px; align-items:flex-start; background:#edf8f1; border-radius:12px; padding:14px 16px; margin-bottom:12px; max-width: 700px; }
	.ky-result-icon { width:34px; height:34px; border-radius:50%; background:#2da44e; color:#fff; display:flex; align-items:center; justify-content:center; flex:0 0 34px; }
	.ky-result-title { font-weight:600; } .ky-result-list { margin:6px 0 0 16px; padding:0; font-size:13px; } .ky-bad { color:#c0341d; }
	.ky-run-hint { color: var(--text-muted); font-size:12px; margin-top:8px; }
	.ky-btn-primary { background:${ORANGE}; color:#fff; border:none; font-weight:600; } .ky-btn-primary:hover { background:#e04300; color:#fff; }
	.ky-btn-soft { background:#fff1ea; color:#c43d00; border:none; margin-right:8px; } .ky-btn-ghost { background:transparent; border:1px solid var(--border-color); }
	.ky-foot { display:flex; align-items:center; justify-content:space-between; padding: 16px 38px; border-top:1px solid var(--border-color); }
	.ky-count { color: var(--text-muted); font-size:12.5px; }
	.ky-switch { display:flex; gap:12px; align-items:flex-start; padding:9px 0; cursor:pointer; max-width:700px; }
	.ky-switch input { display:none; }
	.ky-switch-track { width:38px; height:22px; flex:0 0 38px; border-radius:11px; background:#cfd4d9; position:relative; transition:.2s; margin-top:2px; }
	.ky-switch-thumb { position:absolute; top:2px; left:2px; width:18px; height:18px; border-radius:50%; background:#fff; transition:.2s; }
	.ky-switch input:checked + .ky-switch-track { background:${ORANGE}; } .ky-switch input:checked + .ky-switch-track .ky-switch-thumb { left:18px; }
	.ky-switch-label b { display:block; } .ky-switch-label small { color: var(--text-muted); }
	.ky-plan { max-height: 380px; overflow:auto; border:1px solid var(--border-color); border-radius:10px; }
	.ky-plan table { margin:0; } .ky-plan-section td { background:#f6f6f6; font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
	.ky-check-row { display:block; margin: 12px 0; }
	.ky-copy { margin-bottom: 14px; max-width: 700px; } .ky-copy label { font-size:12px; color: var(--text-muted); }
	.ky-copy-row { display:flex; gap:8px; } .ky-copy-text { font-size:13px; background:#f6f6f6; border-radius:8px; padding:9px 12px; }
	.ky-finish { text-align:center; } .ky-finish-badge { width:64px; height:64px; margin:0 auto 12px; border-radius:50%; background:#2da44e; color:#fff; display:flex; align-items:center; justify-content:center; }
	.ky-summary { max-width: 520px; margin: 18px auto; text-align:left; } .ky-summary-row { display:flex; justify-content:space-between; padding:8px 0; border-bottom:1px solid var(--border-color); }
	@media (max-width: 800px) { .ky-wiz { flex-direction:column; } .ky-rail { width:auto; flex:none; } .ky-grid { grid-template-columns: 1fr; } }
	`;
	const style = document.createElement("style");
	style.id = "ky-wiz-styles";
	style.textContent = css;
	document.head.appendChild(style);
}
