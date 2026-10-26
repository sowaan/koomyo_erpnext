// Copyright (c) 2026, Sowaan and contributors
// For license information, please see license.txt

// Onboarding banner: nudge admins to the setup wizard while the site has no Koomyo connection.
// Dismissible per day.
$(document).on("app_ready", function () {
	const user = frappe.session.user;
	const roles = (frappe.boot && frappe.boot.user && frappe.boot.user.roles) || [];
	if (
		!(
			user === "Administrator" ||
			roles.includes("System Manager") ||
			roles.includes("Koomyo Manager")
		)
	)
		return;
	const dismiss_key = `koomyo_setup_banner_dismissed_${user}_${frappe.datetime.get_today()}`;
	try {
		if (localStorage.getItem(dismiss_key)) return;
	} catch (e) {
		/* storage blocked: show the banner */
	}
	frappe
		.call({ method: "koomyo_erpnext.setup_wizard.get_pending_setup" })
		.then((r) => r && r.message && show_banner(dismiss_key))
		.catch(() => {});
});

function show_banner(dismiss_key) {
	if (document.getElementById("koomyo-setup-banner")) return;
	const orange = "#ff4b00";
	const $banner = $(`
		<div id="koomyo-setup-banner" style="display:flex;align-items:center;gap:14px;background:#1c1c1e;color:#fff;padding:11px 20px;border-radius:12px;margin:12px 16px;box-shadow:0 6px 20px rgba(0,0,0,.18);font-size:13.5px;">
			<img src="/assets/koomyo_erpnext/images/koomyo-icon.png" style="width:34px;height:34px;border-radius:9px;flex:0 0 34px;"/>
			<div style="flex:1;line-height:1.45;">
				<b>${__("Connect your Koomyo restaurant")}</b><br>
				<span style="opacity:.85;">${__(
					"A quick guided setup: paste the API key, and the maps and accounts are filled from Koomyo."
				)}</span>
			</div>
			<button class="btn btn-sm" id="koomyo-banner-setup" style="background:${orange};color:#fff;font-weight:600;border:none;white-space:nowrap;">${__(
		"Set up now"
	)}</button>
			<button class="btn btn-sm" id="koomyo-banner-dismiss" style="background:rgba(255,255,255,.15);color:#fff;border:none;white-space:nowrap;">${__(
				"Later"
			)}</button>
		</div>`);
	const $header = $(".main-section > header");
	if ($header.length) $header.after($banner);
	else $("body").prepend($banner);
	$("#koomyo-banner-setup").on("click", () => {
		$banner.remove();
		frappe.set_route("koomyo-setup");
	});
	$("#koomyo-banner-dismiss").on("click", () => {
		try {
			localStorage.setItem(dismiss_key, "1");
		} catch (e) {
			/* storage blocked */
		}
		$banner.remove();
	});
}
