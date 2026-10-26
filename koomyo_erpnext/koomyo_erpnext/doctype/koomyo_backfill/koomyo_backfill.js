// Copyright (c) 2026, Sowaan and contributors
// For license information, please see license.txt

const RUNNING = ["Queued", "Running", "Previewing", "Stopping"];

frappe.ui.form.on("Koomyo Backfill", {
	refresh(frm) {
		frm.set_query("connection", () => ({ filters: { enabled: 1 } }));
		show_state(frm);
		if (frm.is_new()) return;

		const call = (method, confirm_text) => {
			const go = () =>
				frm.call(method).then((r) => {
					r.message && frappe.show_alert(r.message);
					frm.reload_doc();
				});
			confirm_text ? frappe.confirm(confirm_text, go) : go();
		};

		if (RUNNING.includes(frm.doc.status)) {
			frm.add_custom_button(__("Stop"), () => call("stop"));
			// Follow the progress while it runs.
			setTimeout(() => frm.reload_doc(), 5000);
			return;
		}
		frm.add_custom_button(__("Preview"), () => call("preview"));
		frm.add_custom_button(
			__("Start"),
			() =>
				call(
					"start",
					__(
						"Bring in {0} to {1} for {2}? Documents are posted with their Koomyo dates. Anything already in ERPNext is left as it is.",
						[
							frappe.datetime.str_to_user(frm.doc.from_date),
							frappe.datetime.str_to_user(frm.doc.to_date),
							frm.doc.connection,
						]
					)
				),
			null
		);
		if (frm.doc.failed) {
			frm.add_custom_button(__("Retry Failed"), () => call("retry_failed"));
		}
		if ((frm.doc.chunks || []).length) {
			frm.add_custom_button(__("Re-run a Day"), () => rerun_day(frm));
		}
		if (frm.doc.created || frm.doc.failed || frm.doc.skipped) {
			frm.add_custom_button(__("Event Log"), () =>
				frappe.set_route("List", "Koomyo Event Log", { backfill: frm.doc.name })
			);
		}
	},
});

function show_state(frm) {
	const colors = { Completed: "green", Previewed: "blue", Failed: "red", Stopped: "orange" };
	if (RUNNING.includes(frm.doc.status)) {
		frm.dashboard.set_headline_alert(
			__("{0}: {1}% done", [__(frm.doc.status), frm.doc.progress || 0]),
			"blue"
		);
	} else if (frm.doc.error) {
		frm.dashboard.set_headline_alert(frappe.utils.escape_html(frm.doc.error), "red");
	} else if (colors[frm.doc.status]) {
		frm.dashboard.set_headline_alert(
			__("{0}: {1} found, {2} created, {3} already there, {4} skipped, {5} failed", [
				__(frm.doc.status),
				frm.doc.found || 0,
				frm.doc.created || 0,
				frm.doc.already_there || 0,
				frm.doc.skipped || 0,
				frm.doc.failed || 0,
			]),
			colors[frm.doc.status]
		);
	}
}

function rerun_day(frm) {
	const options = (frm.doc.chunks || []).map((c) => c.chunk);
	frappe.prompt(
		[{ fieldname: "chunk", fieldtype: "Select", label: __("Day"), options, reqd: 1 }],
		(values) =>
			frm.call("rerun_day", { chunk: values.chunk }).then((r) => {
				r.message && frappe.show_alert(r.message);
				frm.reload_doc();
			}),
		__("Re-run one day"),
		__("Run")
	);
}
