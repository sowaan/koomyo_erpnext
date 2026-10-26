// Copyright (c) 2026, Sowaan and contributors
// For license information, please see license.txt

frappe.pages["koomyo-dashboard"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Koomyo Dashboard"),
		single_column: true,
	});
	const board = new KoomyoDashboard(page);
	page.set_primary_action(__("Refresh"), () => board.load(), "refresh");
	page.add_inner_button(__("Event Log"), () => frappe.set_route("List", "Koomyo Event Log"));
	page.add_inner_button(__("Backfill"), () => frappe.set_route("List", "Koomyo Backfill"));
	page.add_inner_button(__("Connections"), () => frappe.set_route("List", "Koomyo Connection"));
	board.load();
	// Keep it current while it is open.
	board.timer = setInterval(() => {
		if (frappe.get_route_str() === "koomyo-dashboard") board.load();
	}, 30000);
};

class KoomyoDashboard {
	constructor(page) {
		this.page = page;
		// Room below the sticky page head, so the first row is not hidden under it.
		this.$body = $('<div class="koomyo-dashboard" style="padding-top: 48px"></div>').appendTo(
			page.main
		);
	}

	load() {
		frappe.call("koomyo_erpnext.dashboard.get_dashboard").then((r) => this.render(r.message));
	}

	render(data) {
		const esc = frappe.utils.escape_html;
		const t = data.totals;
		const card = (label, value, color, route) =>
			`<div class="col-sm-3"><div class="koomyo-card" data-route="${esc(route || "")}"
				style="border:1px solid var(--border-color);border-radius:8px;padding:12px 16px;margin-bottom:12px;cursor:${
					route ? "pointer" : "default"
				}">
				<div class="text-muted small">${label}</div>
				<div style="font-size:24px;font-weight:600;color:var(--${color}-600, inherit)">${value}</div>
			</div></div>`;
		let html = `<div class="row">
			${card(__("Connections needing attention"), t.attention, t.attention ? "red" : "green")}
			${card(__("Failed events"), t.failed, t.failed ? "red" : "green", "failed")}
			${card(__("Waiting to be processed"), t.pending, t.pending ? "orange" : "green", "pending")}
			${card(__("Booked in the last 24 hours"), t.done_today, "blue")}
		</div>`;

		if (!data.connections.length) {
			html += `<p class="text-muted">${__(
				"No connections yet. Open Koomyo Connection and add one per Company."
			)}</p>`;
		}
		for (const c of data.connections) {
			const status_color =
				c.status === "Connected" ? "green" : c.status === "Not Tested" ? "orange" : "red";
			const pill = (text, color) =>
				`<span class="indicator-pill ${color}" style="margin-right:6px">${text}</span>`;
			const link = (label, filters) =>
				`<a href="#" class="koomyo-log" data-filters='${esc(
					JSON.stringify({ connection: c.name, ...filters })
				)}'>${label}</a>`;
			html += `<div style="border:1px solid var(--border-color);border-radius:8px;padding:16px;margin-bottom:16px">
				<div class="flex justify-between align-center" style="margin-bottom:8px">
					<div>
						<a href="/app/koomyo-connection/${encodeURIComponent(
							c.name
						)}" style="font-size:16px;font-weight:600">${esc(c.name)}</a>
						<span class="text-muted"> &middot; ${esc(c.company)}${
				c.restaurant_name ? " &middot; " + esc(c.restaurant_name) : ""
			}</span>
					</div>
					<div>
						${pill(__(c.enabled ? c.status : "Switched off"), c.enabled ? status_color : "gray")}
						${c.dry_run ? pill(__("Dry Run"), "blue") : ""}
					</div>
				</div>
				${c.status_message ? `<p class="text-danger small">${esc(c.status_message)}</p>` : ""}
				<table class="table table-sm table-bordered" style="margin-bottom:8px">
					<tr>
						<th>${__("Last event")}</th><th>${__("Feed read to")}</th><th>${__("Last full catch-up")}</th>
						<th>${__("Oldest waiting")}</th>
					</tr>
					<tr>
						<td>${
							c.last_event
								? esc(c.last_event.event_type) +
								  " &middot; " +
								  frappe.datetime.prettyDate(c.last_event.creation)
								: "-"
						}</td>
						<td>${__("event #{0}", [c.last_seq || 0])}</td>
						<td>${c.last_caught_up_on ? frappe.datetime.prettyDate(c.last_caught_up_on) : "-"}</td>
						<td>${c.oldest_pending ? frappe.datetime.prettyDate(c.oldest_pending) : "-"}</td>
					</tr>
				</table>
				<div class="small">
					${__("Last 24 hours")}: ${link(__("{0} booked", [c.last_day.Done]), { status: "Done" })},
					${link(__("{0} skipped", [c.last_day.Skipped]), { status: "Skipped" })},
					${link(__("{0} failed", [c.last_day.Failed]), { status: "Failed" })}.
					${__("Now")}: ${link(__("{0} waiting", [c.counts.Pending + c.counts.Processing]), {
				status: "Pending",
			})},
					${link(__("{0} failed", [c.counts.Failed]), { status: "Failed" })}${
				c.needs_mapping
					? ` (${link(__("{0} need a map row", [c.needs_mapping]), {
							status: "Failed",
							error_class: "NeedsMapping",
					  })})`
					: ""
			}.
				</div>
				${c.backfills
					.map(
						(b) =>
							`<div class="small" style="margin-top:6px">${__(
								"Backfill"
							)} <a href="/app/koomyo-backfill/${encodeURIComponent(b.name)}">${esc(
								b.name
							)}</a> (${esc(b.from_date)} &ndash; ${esc(b.to_date)}): ${__(
								b.status
							)}, ${b.progress || 0}%</div>`
					)
					.join("")}
				<div style="margin-top:10px">
					${
						c.counts.Failed
							? `<button class="btn btn-xs btn-default koomyo-retry" data-connection="${esc(
									c.name
							  )}">${__("Retry All Failed")}</button>`
							: ""
					}
					${
						c.enabled
							? `<button class="btn btn-xs btn-default koomyo-catchup" data-connection="${esc(
									c.name
							  )}">${__("Catch Up Now")}</button>`
							: ""
					}
				</div>
			</div>`;
		}
		html += `<p class="text-muted small">${__("As of {0}. Refreshes every 30 seconds.", [
			frappe.datetime.str_to_user(data.as_of),
		])}</p>`;
		this.$body.html(html);
		this.bind();
	}

	bind() {
		this.$body.find(".koomyo-log").on("click", (e) => {
			e.preventDefault();
			frappe.set_route(
				"List",
				"Koomyo Event Log",
				JSON.parse($(e.currentTarget).attr("data-filters"))
			);
		});
		this.$body
			.find(".koomyo-card[data-route=failed]")
			.on("click", () => frappe.set_route("List", "Koomyo Event Log", { status: "Failed" }));
		this.$body
			.find(".koomyo-card[data-route=pending]")
			.on("click", () =>
				frappe.set_route("List", "Koomyo Event Log", { status: "Pending" })
			);
		this.$body.find(".koomyo-retry").on("click", (e) => {
			const connection = $(e.currentTarget).attr("data-connection");
			frappe
				.call(
					"koomyo_erpnext.koomyo_erpnext.doctype.koomyo_event_log.koomyo_event_log.retry_all_failed",
					{ connection }
				)
				.then((r) => {
					frappe.show_alert(r.message);
					this.load();
				});
		});
		this.$body.find(".koomyo-catchup").on("click", (e) => {
			const connection = $(e.currentTarget).attr("data-connection");
			frappe
				.call("run_doc_method", {
					dt: "Koomyo Connection",
					dn: connection,
					method: "catch_up_now",
				})
				.then((r) => r.message && frappe.show_alert(r.message));
		});
	}
}
