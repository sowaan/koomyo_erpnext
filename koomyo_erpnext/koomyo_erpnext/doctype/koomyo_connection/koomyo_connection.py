# Copyright (c) 2026, Sowaan and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, get_url, now_datetime, today

from koomyo_erpnext.client import KoomyoClient, validate_base_url
from koomyo_erpnext.exceptions import KoomyoAPIError
from koomyo_erpnext.sync.events import required_scopes, switched_on_types
from koomyo_erpnext.utils import parse_money

CLOCK_DRIFT_WARN_SECONDS = 60


class KoomyoConnection(Document):
	def onload(self):
		from koomyo_erpnext.auto_setup import checklist

		self.set_onload("checklist", checklist(self))
		if self.webhook_token:
			self.webhook_url = webhook_url(self.webhook_token)
		self.events_to_register = events_to_register(self)

	def validate(self):
		self.base_url = validate_base_url(self.base_url)
		if not self.webhook_token:
			self.webhook_token = frappe.generate_hash(length=32)
		self.webhook_url = webhook_url(self.webhook_token)
		self.events_to_register = events_to_register(self)
		if self.key_scopes:
			scopes = {s.strip() for s in self.key_scopes.split(",")}
			self.missing_scopes = ", ".join(s for s in required_scopes(self) if s not in scopes)

		self.set_stock_deduction_start()
		self.reset_test_when_credentials_change()
		self.validate_company_change()
		self.validate_branch_map()
		self.validate_payment_and_tax_maps()
		self.validate_defaults()
		self.validate_unique_restaurant_and_company()
		self.validate_enable()

	def set_stock_deduction_start(self):
		"""A new Stock Deduction setting applies from the next day, so no day is booked both ways."""
		from koomyo_erpnext.sync.order_stock import DAILY, next_business_day

		self.stock_deduction = self.stock_deduction or DAILY
		if self.is_new() or not self.stock_deduction_since:
			self.stock_deduction_since = self.stock_start_date or today()
		elif self.has_value_changed("stock_deduction"):
			# Days up to the change keep the setting that applied to them.
			self.stock_deduction_before = self.get_doc_before_save().stock_deduction or DAILY
			self.stock_deduction_since = next_business_day(self)

	def reset_test_when_credentials_change(self):
		"""A new key or address may point at another restaurant: test again before use."""
		if self.is_new():
			return
		key_changed = self.api_key and not self.is_dummy_password(self.api_key)
		if key_changed or self.has_value_changed("base_url"):
			self.status = "Not Tested"
			self.status_message = _("The API key or address changed. Press Test Connection.")
			self.enabled = 0

	def validate_company_change(self):
		if self.is_new() or not self.has_value_changed("company"):
			return
		if frappe.db.exists("Koomyo Event Log", {"connection": self.name}):
			frappe.throw(
				_(
					"This connection already has events booked in {0}. Make a new connection for another Company."
				).format(frappe.bold(self.get_doc_before_save().company))
			)

	def validate_branch_map(self):
		seen = set()
		for row in self.branch_map:
			row.koomyo_branch_id = (row.koomyo_branch_id or "").strip()
			if row.koomyo_branch_id in seen:
				frappe.throw(
					_("Row {0}: Koomyo branch {1} is mapped twice.").format(row.idx, row.koomyo_branch_id)
				)
			seen.add(row.koomyo_branch_id)
			for doctype, fieldname in (
				("Cost Center", "cost_center"),
				("Warehouse", "warehouse"),
			):
				value = row.get(fieldname)
				if value and frappe.db.get_value(doctype, value, "company") != self.company:
					frappe.throw(
						_("Branch Map row {0}: {1} {2} does not belong to Company {3}.").format(
							row.idx, _(doctype), frappe.bold(value), frappe.bold(self.company)
						)
					)

		# The same Koomyo restaurant may feed two Companies only if each branch goes to one Company.
		if not self.tenant_id or not seen:
			return
		map_row = frappe.qb.DocType("Koomyo Branch Map")
		conn = frappe.qb.DocType("Koomyo Connection")
		clashes = (
			frappe.qb.from_(map_row)
			.join(conn)
			.on(map_row.parent == conn.name)
			.select(map_row.koomyo_branch_id, conn.name)
			.where(map_row.parenttype == "Koomyo Connection")
			.where(conn.tenant_id == self.tenant_id)
			.where(conn.name != (self.name or ""))
			.where(map_row.koomyo_branch_id.isin(list(seen)))
			.run()
		)
		if clashes:
			branch_id, other = clashes[0]
			frappe.throw(
				_(
					"Koomyo branch {0} is already mapped in connection {1} for the same Koomyo restaurant. Each branch can go to one Company only."
				).format(frappe.bold(branch_id), frappe.bold(other))
			)

	def validate_payment_and_tax_maps(self):
		seen = set()
		for row in self.payment_map:
			row.koomyo_method = (row.koomyo_method or "").strip()
			if row.koomyo_method in seen:
				frappe.throw(
					_("Payment Map row {0}: method {1} is mapped twice.").format(row.idx, row.koomyo_method)
				)
			seen.add(row.koomyo_method)
			self.check_account_company(row.account, _("Payment Map row {0}").format(row.idx))
			if row.fee_percent:
				self.check_account_company(row.fee_account, _("Payment Map row {0}").format(row.idx))
		reasons = set()
		for row in self.waste_map:
			if row.reason in reasons:
				frappe.throw(_("Waste Map row {0}: reason {1} is mapped twice.").format(row.idx, row.reason))
			reasons.add(row.reason)
			self.check_account_company(row.expense_account, _("Waste Map row {0}").format(row.idx))
		sources = set()
		for row in self.discount_map:
			if row.source in sources:
				frappe.throw(
					_("Discount Map row {0}: source {1} is mapped twice.").format(row.idx, row.source)
				)
			sources.add(row.source)
			self.check_account_company(row.account, _("Discount Map row {0}").format(row.idx))
		units = set()
		for row in self.uom_map:
			unit = (row.koomyo_unit or "").strip().lower()
			if unit in units:
				frappe.throw(_("UOM Map row {0}: unit {1} is mapped twice.").format(row.idx, row.koomyo_unit))
			units.add(unit)
		for row in self.tax_map:
			if (row.tax_rate or "").strip():
				try:
					parse_money(row.tax_rate.strip())
				except ValueError:
					frappe.throw(
						_("Tax Map row {0}: {1} is not a rate such as 15 or 15.00.").format(
							row.idx, row.tax_rate
						)
					)
			self.check_account_company(row.account_head, _("Tax Map row {0}").format(row.idx))

	def validate_defaults(self):
		for fieldname in (
			"income_account",
			"delivery_fee_account",
			"channel_markup_account",
			"payment_adjustment_account",
			"cash_over_short_account",
			"drawer_account",
			"cogs_account",
			"stock_adjustment_account",
			"transfer_loss_account",
		):
			self.check_account_company(self.get(fieldname), _(self.meta.get_label(fieldname)))
		for fieldname in ("main_warehouse", "transit_warehouse"):
			warehouse = self.get(fieldname)
			if warehouse and frappe.db.get_value("Warehouse", warehouse, "company") != self.company:
				frappe.throw(
					_("{0} {1} does not belong to Company {2}.").format(
						_(self.meta.get_label(fieldname)), warehouse, self.company
					)
				)
		if self.transit_warehouse and any(row.warehouse == self.transit_warehouse for row in self.branch_map):
			frappe.throw(
				_("Transit Warehouse {0} is also on the Branch Map. Use a warehouse of its own.").format(
					self.transit_warehouse
				)
			)
		if self.price_list:
			currency = frappe.db.get_value("Price List", self.price_list, "currency")
			company_currency = frappe.get_cached_value("Company", self.company, "default_currency")
			if currency != company_currency:
				frappe.throw(
					_("Price List {0} is in {1}; Company {2} works in {3}.").format(
						self.price_list, currency, self.company, company_currency
					)
				)
		if (
			self.default_cost_center
			and frappe.db.get_value("Cost Center", self.default_cost_center, "company") != self.company
		):
			frappe.throw(
				_("{0} does not belong to Company {1}.").format(self.default_cost_center, self.company)
			)

	def check_account_company(self, account, where):
		if account and frappe.db.get_value("Account", account, "company") != self.company:
			frappe.throw(
				_("{0}: Account {1} does not belong to Company {2}.").format(
					where, frappe.bold(account), frappe.bold(self.company)
				)
			)

	def validate_unique_restaurant_and_company(self):
		if not self.tenant_id:
			return
		other = frappe.db.get_value(
			"Koomyo Connection",
			{"tenant_id": self.tenant_id, "company": self.company, "name": ("!=", self.name or "")},
			"name",
		)
		if other:
			frappe.throw(
				_("Connection {0} already links this Koomyo restaurant to Company {1}.").format(
					frappe.bold(other), frappe.bold(self.company)
				)
			)

	def validate_enable(self):
		if not self.enabled:
			return
		if self.status == "Not Tested" or not self.tenant_id:
			frappe.throw(_("Press Test Connection before you switch the connection on."))
		problem = currency_problem(self.company, self.koomyo_currency)
		if problem:
			frappe.throw(problem)

	# ------------------------------------------------------------ actions

	@frappe.whitelist()
	def test_connection(self):
		self.check_permission("write")
		result = {"ok": False, "warnings": [], "branches": []}
		try:
			client = KoomyoClient.for_connection(self)
			account = client.get_account()
		except KoomyoAPIError as e:
			self.status = "Needs Attention"
			self.status_message = connection_error_message(e)
			self.last_tested_on = now_datetime()
			self.save()
			result["error"] = self.status_message
			return result

		if (
			self.tenant_id
			and self.tenant_id != account["tenant_id"]
			and frappe.db.exists("Koomyo Event Log", {"connection": self.name})
		):
			self.status = "Needs Attention"
			self.status_message = _(
				"This key belongs to another Koomyo restaurant ({0}) than the one this connection already booked. Use a key of {1}, or make a new connection."
			).format(account["name"], self.restaurant_name)
			self.enabled = 0
			self.save()
			result["error"] = self.status_message
			return result

		scopes = sorted(account["key"]["scopes"])
		missing = [s for s in required_scopes(self) if s not in scopes]
		self.restaurant_name = account["name"]
		self.tenant_id = account["tenant_id"]
		self.koomyo_currency = account["currency"]
		self.koomyo_timezone = account["timezone"]
		self.key_name = account["key"].get("name")
		self.key_scopes = ", ".join(scopes)
		self.missing_scopes = ", ".join(missing)
		self.last_tested_on = now_datetime()

		problems = []
		currency = currency_problem(self.company, self.koomyo_currency)
		if currency:
			problems.append(currency)
		if missing:
			problems.append(
				_(
					"The API key is missing these scopes: {0}. Add them in Koomyo Settings > Integrations > API keys."
				).format(", ".join(missing))
			)

		drift = clock_drift_seconds(account.get("server_time"))
		if drift is not None and abs(drift) > CLOCK_DRIFT_WARN_SECONDS:
			result["warnings"].append(
				_(
					"This server's clock is {0} seconds off Koomyo's. Webhook signatures fail beyond 300 seconds. Check NTP."
				).format(int(drift))
			)

		if "catalog.read" in scopes:
			try:
				mapped = {row.koomyo_branch_id for row in self.branch_map}
				for branch in client.list_branches():
					result["branches"].append(
						{
							"id": branch.get("id"),
							"name": branch.get("name"),
							"code": branch.get("code"),
							"active": branch.get("active"),
							"mapped": branch.get("id") in mapped,
						}
					)
				unmapped = [b["name"] for b in result["branches"] if not b["mapped"] and b["active"]]
				if unmapped:
					result["warnings"].append(
						_("These Koomyo branches are not mapped yet: {0}.").format(", ".join(unmapped))
					)
			except KoomyoAPIError as e:
				result["warnings"].append(connection_error_message(e))

		if self.sync_purchase_requests and "purchasing.read" in scopes:
			try:
				client.get("/purchase-requests", {"limit": 1})
			except KoomyoAPIError as e:
				if e.status == 403 and e.code in ("feature_not_available", "app_not_enabled"):
					from koomyo_erpnext.sync.purchase_requests import feature_message

					result["warnings"].append(feature_message(e))
				else:
					result["warnings"].append(connection_error_message(e))

		if problems:
			self.status = "Needs Attention"
			self.status_message = " ".join(problems)
		elif self.status != "Needs Re-sync":
			# A feed gap is not fixed by a good key; it stays until Restart Feed.
			self.status = "Connected"
			self.status_message = ""
		if problems:
			self.enabled = 0
		self.save()

		result.update(
			{
				"ok": not problems,
				"error": " ".join(problems),
				"restaurant": account["name"],
				"currency": account["currency"],
				"timezone": account["timezone"],
				"scopes": scopes,
				"missing_scopes": missing,
			}
		)
		return result

	@frappe.whitelist()
	def catch_up_now(self):
		self.check_permission("write")
		if not self.enabled:
			frappe.throw(_("Switch the connection on first."))
		frappe.enqueue(
			"koomyo_erpnext.sync.events.catch_up",
			queue="long",
			job_id=f"koomyo-catchup-{frappe.local.site}-{self.name}",
			deduplicate=True,
			connection_name=self.name,
		)
		return _("Reading the Koomyo events feed in the background. Watch the Event Log.")

	@frappe.whitelist()
	def book_dry_run_events(self):
		"""After a dry run: queue every event that was only checked, so it is booked for real."""
		self.check_permission("write")
		if self.dry_run:
			frappe.throw(_("Switch Dry Run off first."))
		from koomyo_erpnext.sync.events import enqueue_processing, reset_for_retry

		names = frappe.get_all(
			"Koomyo Event Log",
			filters={"connection": self.name, "dry_run": 1, "status": "Skipped"},
			pluck="name",
			order_by="seq asc",
		)
		for name in names:
			reset_for_retry(frappe.get_doc("Koomyo Event Log", name))
		if names:
			enqueue_processing(self.name)
		return _("{0} event(s) queued to be booked.").format(len(names))

	@frappe.whitelist()
	def auto_setup_plan(self):
		"""Read Koomyo and propose the maps and defaults. Writes nothing."""
		self.check_permission("write")
		from koomyo_erpnext.auto_setup import build_plan, safe_read

		koomyo, error = safe_read(self)
		if error:
			frappe.throw(error)
		return {
			"restaurant": koomyo["account"]["name"],
			"notes": koomyo["notes"],
			"rows": build_plan(self, koomyo),
		}

	@frappe.whitelist()
	def auto_setup_apply(self, choices, load_menu: int = 0):
		"""Create and fill what the user chose. Fills empty settings only; never overwrites or deletes."""
		self.check_permission("write")
		from koomyo_erpnext.auto_setup import apply_plan, safe_read

		koomyo, error = safe_read(self)
		if error:
			frappe.throw(error)
		result = apply_plan(self, koomyo, frappe.parse_json(choices))
		if int(load_menu):
			self.reload()
			self.load_catalog(preview=0)
			result["menu"] = _("The menu and ingredients are loading in the background.")
		return result

	@frappe.whitelist()
	def undo_auto_setup(self):
		"""Remove the records Set Up From Koomyo made that nothing uses; keep the rest."""
		self.check_permission("write")
		from koomyo_erpnext.auto_setup import undo_plan

		return undo_plan(self)

	@frappe.whitelist()
	def load_catalog(self, preview: int = 0):
		"""Queue the menu and ingredient load (or its preview) for this connection."""
		self.check_permission("write")
		if self.status == "Not Tested" or not self.tenant_id:
			frappe.throw(_("Press Test Connection first."))
		for fieldname in ("menu_item_group", "ingredient_item_group"):
			if not self.get(fieldname):
				frappe.throw(_("Set {0} first.").format(_(self.meta.get_label(fieldname))))
		if not self.uom_map:
			frappe.throw(_("Fill the UOM Map first: ingredients need their Koomyo unit mapped to a UOM."))
		frappe.enqueue(
			"koomyo_erpnext.sync.catalog.load_catalog",
			queue="long",
			timeout=3600,
			job_id=f"koomyo-catalog-{frappe.local.site}-{self.name}",
			deduplicate=True,
			connection_name=self.name,
			preview=bool(int(preview)),
		)
		return _(
			"Reading Koomyo's menu and ingredients in the background. The result shows under Menu and Ingredients."
		)

	@frappe.whitelist()
	def restart_feed(self):
		"""After a gap longer than the feed keeps: start reading again from today.

		Events before today must come in with a backfill; the duplicate guard makes overlaps safe.
		"""
		self.check_permission("write")
		self.last_seq = 0
		self.last_caught_up_on = None
		self.start_from_date = today()
		if self.status == "Needs Re-sync":
			self.status = "Connected"
			self.status_message = ""
		self.save()
		return _("The feed will be read again from today. Bring earlier days in with a backfill.")


# ----------------------------------------------------------------- helpers


def webhook_url(token: str) -> str:
	return get_url(f"/api/method/koomyo_erpnext.api.webhook?c={token}")


def events_to_register(connection) -> str:
	types = switched_on_types(connection)
	if not types:
		return _(
			"None yet. Switch on a sync below; you can add the endpoint now and press Send test in Koomyo."
		)
	return ", ".join(types)


def currency_problem(company: str, koomyo_currency: str | None) -> str | None:
	if not koomyo_currency:
		return None
	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	if company_currency and company_currency != koomyo_currency:
		return _(
			"The Koomyo restaurant works in {0} but Company {1} works in {2}. This app does not convert currencies; pick a Company in {0}."
		).format(koomyo_currency, company, company_currency)
	return None


def clock_drift_seconds(server_time) -> float | None:
	if not isinstance(server_time, str) or not server_time:
		return None
	try:
		theirs = get_datetime(server_time.replace("Z", "+00:00"))
	except Exception:
		return None
	from datetime import datetime, timezone

	return (datetime.now(timezone.utc) - theirs).total_seconds()


def connection_error_message(e: KoomyoAPIError) -> str:
	messages = {
		"missing_api_key": _(
			"No API key is saved. Paste the key from Koomyo Settings > Integrations > API keys."
		),
		"invalid_api_key": _(
			"Koomyo does not accept this API key. It may be revoked. Make a new key in Koomyo and paste it here."
		),
		"ip_not_allowed": _(
			"The API key has an IP allowlist that does not include this server. Add this server's address to the key in Koomyo."
		),
		"app_not_enabled": _(
			"The Integrations app is switched off for this restaurant in Koomyo. Switch it on in the Koomyo admin."
		),
		"feature_not_available": _("The restaurant's Koomyo plan does not include this part of the API."),
		"too_many_failed_attempts": _(
			"Koomyo is blocking this server for a minute after too many calls with a wrong key. Fix the key, then wait a minute."
		),
		"rate_limited": _("Koomyo asked to slow down. Try again in a minute."),
		"network_error": _(
			"Could not reach Koomyo at the API address. Check the address and this server's internet access."
		),
		"unexpected_redirect": _("Koomyo answered with a redirect. Check the API address."),
		"bad_response": _("Koomyo answered in an unexpected shape. Check the API address."),
	}
	text = messages.get(e.code) or _("Koomyo answered: {0}").format(e.message)
	if e.request_id:
		text += " " + _("(Koomyo request id {0})").format(e.request_id)
	return text
