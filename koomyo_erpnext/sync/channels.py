"""Sales channels: a partner (Foodpanda, Careem, ...) whose orders the restaurant punches in at its own counter.

Docs: docs.koomyo.com/guides/sales-channels, events/sales (order.paid, sales_channel.payout_recorded,
sales_channel.payout_updated), OpenAPI SalesChannel, SalesChannelPayout and the `sales_channel` block of an Order.
Channels are the restaurant's own list: nothing here names a partner. Everything is driven by the channel id.

How the money is booked (the partner owes the restaurant, so the partner is the customer):

- each channel, the first time it is seen, gets a Customer and a receivable Account named from its name and code
  (a row of the connection's Channel Map remembers them by channel id; a renamed channel keeps its row);
- a paid order is a Sales Invoice for the full sale to that Customer, on that receivable, so no cash or bank payment
  is booked: the partner pays later;
- the commission is one Journal Entry per order: debit the commission expense (and the tax on it), credit the
  receivable against the invoice. What is left on the receivable is the order's `net_amount`;
- an order paid before commissions were recorded has all three figures null: it counts at its full total, with no
  commission (the guide says so too), so the receivable matches the channel's `balance.owed`;
- a refund is a Credit Note on the same receivable plus the same share of the commission given back, so what the
  partner owes falls by the refunded share of the net (never the whole refund);
- a payout is one Payment Entry from the receivable to the bank for `amount_received`, the adjustments as its
  deductions; an adjustment that arrives later is a Journal Entry on the same receivable; a void cancels them.

Never written to Koomyo: the sales channel API is read only. Money is Decimal throughout.
"""

import json
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import now_datetime, nowdate

from koomyo_erpnext.client import KoomyoClient
from koomyo_erpnext.exceptions import Fatal, KoomyoAPIError, NeedsMapping, Skipped
from koomyo_erpnext.sync.common import (
	branch_row,
	cents,
	check_company,
	doc_money,
	find_existing,
	money,
	setting,
	stamp,
	to_field,
)
from koomyo_erpnext.sync.events import handles

SCOPES = ("orders.read", "sales_channels.read")
EVENT_LOG = "Koomyo Event Log"
CHECK_TYPE = "koomyo.channel_check"
COMMISSION = "commission"
COMMISSION_TAX = "commission tax"


def channel_of(order: dict) -> dict | None:
	"""The order's `sales_channel` block, or None for an ordinary order."""
	block = order.get("sales_channel")
	return block if isinstance(block, dict) and block.get("id") else None


# ------------------------------------------------------------------ the party of a channel


def map_row(connection, channel_id: str):
	return frappe.db.get_value(
		"Koomyo Channel Map",
		{"parent": connection.name, "parenttype": "Koomyo Connection", "channel_id": channel_id},
		[
			"name",
			"channel_code",
			"channel_name",
			"customer",
			"receivable_account",
			"commission_account",
			"payout_account",
		],
		as_dict=True,
	)


def party_for(connection, channel: dict):
	"""The Customer and receivable of a channel, made on first sight (never twice). A new name or code is kept up to date."""
	channel_id = channel.get("id")
	if not channel_id:
		raise Fatal(_("Koomyo sent a sales channel without an id."))
	row = map_row(connection, channel_id)
	if row:
		code, name = channel.get("code"), channel.get("name")
		if (code and code != row.channel_code) or (name and name != row.channel_name):
			frappe.db.set_value(
				"Koomyo Channel Map",
				row.name,
				{"channel_code": code or row.channel_code, "channel_name": name or row.channel_name},
			)
		check_company(
			connection, "Account", row.receivable_account, _("Channel {0}").format(row.channel_code)
		)
		return row
	customer, account = make_party(connection, channel)
	idx = (
		frappe.db.count("Koomyo Channel Map", {"parent": connection.name, "parenttype": "Koomyo Connection"})
		+ 1
	)
	frappe.get_doc(
		{
			"doctype": "Koomyo Channel Map",
			"parent": connection.name,
			"parenttype": "Koomyo Connection",
			"parentfield": "channel_map",
			"idx": idx,
			"channel_id": channel_id,
			"channel_code": channel.get("code"),
			"channel_name": channel.get("name"),
			"customer": customer,
			"receivable_account": account,
		}
	).insert(ignore_permissions=True)
	return map_row(connection, channel_id)


def make_party(connection, channel: dict) -> tuple[str, str]:
	"""(Customer, receivable Account) of a channel, made if they do not exist yet. No Channel Map row is written."""
	if not channel.get("id"):
		raise Fatal(_("Koomyo sent a sales channel without an id."))
	label = _label(channel)
	account = receivable_account(connection, label)
	return channel_customer(connection, channel, label, account), account


def _label(channel: dict) -> str:
	name = (channel.get("name") or channel.get("code") or channel["id"]).strip()
	code = channel.get("code")
	return f"{name} ({code})" if code and code != name else name


def receivable_account(connection, label: str) -> str:
	"""A receivable Account for the channel, next to the Company's own Debtors account."""
	default = frappe.get_cached_value("Company", connection.company, "default_receivable_account")
	if not default:
		raise NeedsMapping(
			_(
				"Company {0} has no Default Receivable Account, so a receivable for the channel {1} cannot be made."
			).format(connection.company, label)
		)
	account_name = f"{label} Receivable"[:140]
	existing = frappe.db.get_value("Account", {"company": connection.company, "account_name": account_name})
	if existing:
		return existing
	account = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": account_name,
			"company": connection.company,
			"parent_account": frappe.db.get_value("Account", default, "parent_account"),
			"account_type": "Receivable",
			"root_type": "Asset",
			"is_group": 0,
		}
	)
	account.insert(ignore_permissions=True)
	return account.name


def channel_customer(connection, channel: dict, label: str, account: str) -> str:
	koomyo_id = f"channel:{channel['id']}"
	existing = find_existing("Customer", connection, koomyo_id)
	if existing:
		return existing
	name = label[:140]
	if frappe.db.exists("Customer", {"customer_name": name}):
		name = f"{label} [{channel['id'][:8]}]"[:140]  # another record already uses the name
	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": name,
			"customer_type": "Company",
			"customer_group": leaf("Customer Group", connection.get("customer_group"), "customer_group"),
			"territory": leaf("Territory", connection.get("territory"), "territory"),
			"accounts": [{"company": connection.company, "account": account}],
		}
	)
	stamp(customer, connection, koomyo_id)
	customer.insert(ignore_permissions=True)
	return customer.name


def leaf(doctype: str, chosen: str | None, selling_setting: str) -> str:
	"""A Customer must sit in a group that is not a folder: the connection's choice, Selling Settings, else the first leaf."""
	for name in (chosen, frappe.db.get_single_value("Selling Settings", selling_setting)):
		if name and not frappe.db.get_value(doctype, name, "is_group"):
			return name
	leaf_name = frappe.db.get_value(doctype, {"is_group": 0}, "name", order_by="creation asc")
	if not leaf_name:
		raise NeedsMapping(
			_("There is no {0} to put the channel's Customer in. Make one, then press Retry.").format(
				_(doctype)
			)
		)
	return leaf_name


def commission_account(connection, row) -> str:
	account = row.get("commission_account") or setting(
		connection, "channel_commission_account", _("Channel Commission Account")
	)
	check_company(connection, "Account", account, _("Channel {0}").format(row.channel_code))
	return account


def tax_account(connection) -> str:
	account = setting(connection, "channel_commission_tax_account", _("Tax on Commission Account"))
	check_company(connection, "Account", account, _("Tax on Commission Account"))
	return account


# ------------------------------------------------------------------ journal entries on the receivable


def post_entry(connection, koomyo_id: str, date, rows: list[dict], remark: str, branch_id=None):
	entry = frappe.new_doc("Journal Entry")
	entry.update(
		{
			"voucher_type": "Journal Entry",
			"company": connection.company,
			"posting_date": date,
			"user_remark": remark[:1000],
		}
	)
	for row in rows:
		entry.append("accounts", row)
	stamp(entry, connection, koomyo_id, branch_id)
	entry.insert(ignore_permissions=True)
	entry.submit()
	return entry


def line(account, debit: Decimal = Decimal(0), credit: Decimal = Decimal(0), **more) -> dict:
	return {
		"account": account,
		"debit_in_account_currency": to_field(debit),
		"credit_in_account_currency": to_field(credit),
		**more,
	}


def commission_figures(order: dict, channel: dict):
	"""(commission, tax) of a paid order, or None when Koomyo had not recorded them (the order counts at its full total)."""
	raw = [channel.get(k) for k in ("commission_amount", "commission_tax", "net_amount")]
	if all(v is None for v in raw):
		return None
	if any(v is None for v in raw):
		raise Fatal(
			_(
				"Order {0}: Koomyo gave only some of the commission figures (commission {1}, tax {2}, net {3}). Nothing was booked."
			).format(order.get("number"), *raw)
		)
	commission = money(raw[0], "commission_amount")
	tax = money(raw[1], "commission_tax")
	net = money(raw[2], "net_amount")
	total = money((order.get("amounts") or {}).get("total"), "amounts.total")
	if commission < 0 or tax < 0 or total - commission - tax != net:
		raise Fatal(
			_(
				"Order {0}: the total {1} less commission {2} and its tax {3} is not the net {4} Koomyo states. Nothing was booked."
			).format(order.get("number"), total, commission, tax, net)
		)
	return commission, tax


def book_commission(connection, invoice, order: dict, channel: dict, row):
	"""The commission of a paid order: debit the expense (and its tax), credit the receivable against the invoice."""
	figures = commission_figures(order, channel)
	if not figures or not (figures[0] or figures[1]):
		return None
	commission, tax = figures
	koomyo_id = f"{COMMISSION}:{order['id']}"
	existing = find_existing("Journal Entry", connection, koomyo_id)
	if existing:
		return existing
	rows = []
	if commission:
		rows.append(
			line(
				commission_account(connection, row),
				debit=commission,
				cost_center=invoice.cost_center,
				user_remark=COMMISSION,
			)
		)
	if tax:
		rows.append(
			line(
				tax_account(connection),
				debit=tax,
				cost_center=invoice.cost_center,
				user_remark=COMMISSION_TAX,
			)
		)
	rows.append(
		line(
			row.receivable_account,
			credit=commission + tax,
			party_type="Customer",
			party=row.customer,
			reference_type="Sales Invoice",
			reference_name=invoice.name,
		)
	)
	entry = post_entry(
		connection,
		koomyo_id,
		invoice.posting_date,
		rows,
		_("Koomyo order {0}: commission of {1} ({2}) and tax {3}.").format(
			order.get("number"), commission, row.channel_name, tax
		),
		invoice.koomyo_branch,
	)
	return entry.name


def reverse_commission(connection, original, credit_note, refund: dict):
	"""A refund gives back the same share of the commission, so the partner owes less by the refunded share of the net."""
	order_id = refund["order_id"]
	booked = find_existing("Journal Entry", connection, f"{COMMISSION}:{order_id}", include_cancelled=False)
	if not booked:
		return None  # an order that counted at its full total has no commission to give back
	figures = {
		r.user_remark: Decimal(str(r.debit))
		for r in frappe.get_all(
			"Journal Entry Account", filters={"parent": booked}, fields=["user_remark", "debit"]
		)
		if r.user_remark in (COMMISSION, COMMISSION_TAX)
	}
	invoice_total = doc_money(original.grand_total)
	refunded = -sum(
		(
			doc_money(gt)
			for gt in frappe.get_all(
				"Sales Invoice",
				filters={"return_against": original.name, "is_return": 1, "docstatus": 1},
				pluck="grand_total",
			)
		),
		Decimal(0),
	)  # includes this credit note, which is already submitted
	already = {COMMISSION: Decimal(0), COMMISSION_TAX: Decimal(0)}
	for name in frappe.get_all(
		"Journal Entry",
		filters={
			"koomyo_connection": connection.name,
			"koomyo_id": ("like", f"{COMMISSION}-refund:{order_id}:%"),
			"docstatus": 1,
		},
		pluck="name",
	):
		for r in frappe.get_all(
			"Journal Entry Account", filters={"parent": name}, fields=["user_remark", "credit"]
		):
			if r.user_remark in already:
				already[r.user_remark] += Decimal(str(r.credit))
	back = {
		kind: cents(figures.get(kind, Decimal(0)) * refunded / invoice_total) - already[kind]
		for kind in (COMMISSION, COMMISSION_TAX)
	}
	if not (back[COMMISSION] or back[COMMISSION_TAX]):
		return None
	row = map_row(connection, channel_id_of(original))
	rows = []
	if back[COMMISSION]:
		rows.append(
			line(
				commission_account(connection, row),
				credit=back[COMMISSION],
				cost_center=credit_note.cost_center,
				user_remark=COMMISSION,
			)
		)
	if back[COMMISSION_TAX]:
		rows.append(
			line(
				tax_account(connection),
				credit=back[COMMISSION_TAX],
				cost_center=credit_note.cost_center,
				user_remark=COMMISSION_TAX,
			)
		)
	rows.append(
		line(
			row.receivable_account,
			debit=back[COMMISSION] + back[COMMISSION_TAX],
			party_type="Customer",
			party=row.customer,
		)
	)
	entry = post_entry(
		connection,
		f"{COMMISSION}-refund:{order_id}:{refund['id']}",
		credit_note.posting_date,
		rows,
		_("Koomyo refund for order {0}: commission {1} and tax {2} given back.").format(
			refund.get("order_number"), back[COMMISSION], back[COMMISSION_TAX]
		),
		credit_note.koomyo_branch,
	)
	return entry.name


def channel_id_of(invoice) -> str | None:
	"""The channel id of a booked channel invoice, from its customer's `channel:<id>` stamp."""
	customer_id = frappe.db.get_value("Customer", invoice.customer, "koomyo_id") or ""
	return customer_id.removeprefix("channel:") if customer_id.startswith("channel:") else None


def is_channel_invoice(invoice) -> bool:
	return bool(invoice.get("koomyo_channel"))


# ------------------------------------------------------------------ closing the invoices


def reconcile_party(connection, row) -> int:
	"""Match what the partner paid (payouts, adjustments, credit notes) to its oldest open invoices.

	A payout is one amount for a period and Koomyo does not say which orders it covers, so it is booked on
	account; this uses ERPNext's own Payment Reconciliation (oldest invoice first), so each invoice shows how
	much of it is still owed and the totals on the receivable never change. Safe to repeat: with nothing open
	or nothing unmatched it does nothing. A problem here never undoes the payout that was booked. Returns how
	many matches were made.
	"""
	savepoint = "koomyo_channel_reconcile"
	frappe.db.savepoint(savepoint)
	try:
		pr = frappe.get_doc(
			{
				"doctype": "Payment Reconciliation",
				"company": connection.company,
				"party_type": "Customer",
				"party": row.customer,
				"receivable_payable_account": row.receivable_account,
			}
		)
		pr.get_unreconciled_entries()
		if not (pr.invoices and pr.payments):
			return 0
		pr.allocate_entries(
			frappe._dict(
				{
					"invoices": [x.as_dict() for x in pr.invoices],
					"payments": [x.as_dict() for x in pr.payments],
				}
			)
		)
		matches = len(pr.get("allocation") or [])
		if matches:
			pr.reconcile()
		return matches
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(title=f"Koomyo channel reconcile {row.channel_code}")
		return 0


# ------------------------------------------------------------------ payouts


@handles("sales_channel.payout_recorded", "sales_channel.payout_updated", switch="sync_sales", scopes=SCOPES)
def on_payout(event: dict, connection):
	return sync_payout(event["data"], connection)


def sync_payout(payout: dict, connection, client: KoomyoClient | None = None):
	"""One Payment Entry per payout (keyed by the payout id). Voided: cancelled. A later adjustment: a Journal Entry.

	`payout_updated` carries the whole payout as it is now, so the same function serves both events.
	"""
	if payout.get("object") != "sales_channel_payout" or not payout.get("id") or not payout.get("channel_id"):
		raise Fatal(_("Koomyo sent something that is not a sales channel payout."))
	existing = find_existing("Payment Entry", connection, payout["id"])
	if payout.get("status") == "voided":
		return void_payout(connection, payout, existing)
	if payout.get("status") != "recorded":
		raise Fatal(_("Payout {0} has the status {1!r}.").format(payout["id"], payout.get("status")))
	if existing and frappe.db.get_value("Payment Entry", existing, "docstatus") == 2:
		raise Skipped(_("Payout {0} was voided and cannot be recorded again.").format(payout["id"]))
	row = map_row(connection, payout["channel_id"]) or party_for(
		connection, channel_from(connection, payout, client)
	)
	if existing:
		return top_up_adjustments(connection, payout, row, existing)
	amount = money(payout.get("amount_received"), "amount_received")
	if amount <= 0:
		raise Skipped(_("Payout {0} received {1}: nothing to book.").format(payout["id"], amount))
	adjustments = adjustments_of(payout)
	pe = new_payout_entry(connection, payout, row, amount, adjustments)
	reconcile_party(connection, row)
	return ("Payment Entry", pe)


def channel_from(connection, payout: dict, client) -> dict:
	"""A payout of a channel that was never seen: read the channel (name and code) once."""
	client = client or KoomyoClient.for_connection(connection)
	try:
		channel = client.get("/sales-channels/" + payout["channel_id"])
	except KoomyoAPIError:
		return {
			"id": payout["channel_id"],
			"code": payout.get("channel_code"),
			"name": payout.get("channel_code"),
		}
	return channel


def adjustments_of(payout: dict) -> list[tuple[str, str, Decimal]]:
	rows = []
	for adj in payout.get("adjustments") or []:
		if not adj.get("id"):
			raise Fatal(_("Payout {0} has an adjustment without an id.").format(payout["id"]))
		rows.append(
			(adj["id"], adj.get("label") or _("Adjustment"), money(adj.get("amount"), "adjustment amount"))
		)
	return rows


def new_payout_entry(connection, payout: dict, row, amount: Decimal, adjustments) -> str:
	bank = row.get("payout_account") or setting(
		connection, "channel_payout_account", _("Payout Bank Account")
	)
	check_company(connection, "Account", bank, _("Channel {0}").format(row.channel_code))
	cost_center = branch_row(connection, None).cost_center
	date = payout.get("received_on") or nowdate()
	reference = (payout.get("reference") or payout["id"])[:140]
	pe = frappe.new_doc("Payment Entry")
	pe.update(
		{
			"payment_type": "Receive",
			"company": connection.company,
			"posting_date": date,
			"party_type": "Customer",
			"party": row.customer,
			"paid_from": row.receivable_account,
			"paid_to": bank,
			"paid_amount": to_field(amount),
			"received_amount": to_field(amount),
			"source_exchange_rate": 1,
			"target_exchange_rate": 1,
			"reference_no": reference,
			"reference_date": date,
			"cost_center": cost_center,
			"custom_remarks": 1,
			"remarks": _("Koomyo payout {0} from {1}, {2} to {3}.").format(
				reference, row.channel_name, payout.get("period_from") or "-", payout.get("period_to") or "-"
			),
		}
	)
	if adjustments:
		adjustment_account = setting(connection, "channel_adjustment_account", _("Payout Adjustment Account"))
		check_company(connection, "Account", adjustment_account, _("Payout Adjustment Account"))
		for adj_id, label, adj_amount in adjustments:
			pe.append(
				"deductions",
				{
					"account": adjustment_account,
					"cost_center": cost_center,
					"amount": to_field(adj_amount),
					"description": f"{label} [{adj_id}]"[:140],
				},
			)
	stamp(pe, connection, payout["id"])
	pe.insert(ignore_permissions=True)
	pe.submit()
	return pe.name


def top_up_adjustments(connection, payout: dict, row, payment_entry: str):
	"""An adjustment recorded after the payout: its own Journal Entry on the receivable (the Payment Entry is submitted)."""
	deducted = " ".join(
		frappe.get_all("Payment Entry Deduction", filters={"parent": payment_entry}, pluck="description")
	)
	adjustments = [a for a in adjustments_of(payout) if f"[{a[0]}]" not in deducted]
	if not adjustments:
		reconcile_party(connection, row)  # a payout booked before this existed is closed on the next event
		return ("Payment Entry", payment_entry)
	account = setting(connection, "channel_adjustment_account", _("Payout Adjustment Account"))
	check_company(connection, "Account", account, _("Payout Adjustment Account"))
	cost_center = branch_row(connection, None).cost_center
	date = payout.get("received_on") or nowdate()
	for adj_id, label, amount in adjustments:
		if find_existing("Journal Entry", connection, f"adjustment:{adj_id}"):
			continue
		post_entry(
			connection,
			f"adjustment:{adj_id}",
			date,
			[
				line(account, debit=amount, cost_center=cost_center),
				line(
					row.receivable_account,
					credit=amount,
					party_type="Customer",
					party=row.customer,
				),
			],
			_("Koomyo payout {0}: {1} kept back by {2}.").format(
				payout.get("reference") or payout["id"], label, row.channel_name
			),
		)
	reconcile_party(connection, row)
	return ("Payment Entry", payment_entry)


def void_payout(connection, payout: dict, payment_entry: str | None):
	if not payment_entry:
		raise Skipped(_("Payout {0} was voided and had never been booked.").format(payout["id"]))
	for adj in payout.get("adjustments") or []:
		entry = find_existing(
			"Journal Entry", connection, f"adjustment:{adj.get('id')}", include_cancelled=False
		)
		if entry:
			frappe.get_doc("Journal Entry", entry).cancel()
	pe = frappe.get_doc("Payment Entry", payment_entry)
	if pe.docstatus == 1:
		pe.cancel()
	return ("Payment Entry", payment_entry)


# ------------------------------------------------------------------ the nightly check


def ledger_balance(account: str, customer: str) -> Decimal:
	"""What the channel owes according to ERPNext: debits less credits on its receivable for its Customer."""
	total = frappe.get_all(
		"GL Entry",
		filters={"account": account, "party_type": "Customer", "party": customer, "is_cancelled": 0},
		fields=["sum(debit) as debit", "sum(credit) as credit"],
	)[0]
	return cents(Decimal(str(total.debit or 0)) - Decimal(str(total.credit or 0)))


def schedule_checks():
	"""Daily: one job per connection that has sales on (its own queue slot, never one loop over all)."""
	for name in frappe.get_all("Koomyo Connection", filters={"enabled": 1, "sync_sales": 1}, pluck="name"):
		frappe.enqueue(
			"koomyo_erpnext.sync.channels.check_connection",
			queue="long",
			connection_name=name,
			job_id=f"koomyo-channel-check-{frappe.local.site}-{name}",
			deduplicate=True,
		)


def check_connection(connection_name: str, client: KoomyoClient | None = None, dry_run: bool = False) -> dict:
	"""Each channel's receivable must equal its `balance.owed`. A mismatch is a missed event: catch up, then check again."""
	connection = frappe.get_doc("Koomyo Connection", connection_name)
	summary = {"connection": connection.name, "channels": [], "errors": [], "caught_up": 0}
	if not (connection.enabled and connection.sync_sales):
		return summary
	client = client or KoomyoClient.for_connection(connection)
	try:
		channels = list(client.iter_list("/sales-channels", {}))
	except KoomyoAPIError as e:
		summary["errors"].append(str(e)[:300])
		return summary
	for channel in channels:
		result = check_channel(connection, client, channel, dry_run)
		summary["channels"].append(result)
		summary["caught_up"] += result.get("caught_up", 0)
	if not dry_run:
		frappe.db.commit()
	return summary


def owed_by(client, channel_id: str) -> Decimal:
	full = client.get("/sales-channels/" + channel_id)
	return money((full.get("balance") or {}).get("owed"), "balance.owed")


def ledger_of(connection, channel_id: str) -> Decimal:
	row = map_row(connection, channel_id)
	return ledger_balance(row.receivable_account, row.customer) if row else Decimal(0)


def check_channel(connection, client, channel: dict, dry_run: bool, raise_attention: bool = True) -> dict:
	result = {"channel": channel.get("code"), "id": channel["id"], "status": "ok", "caught_up": 0}
	try:
		owed = owed_by(client, channel["id"])
	except (KoomyoAPIError, Fatal) as e:
		result.update(status="unreadable", error=str(e)[:300])
		return result
	row = map_row(connection, channel["id"])
	if row and not dry_run:
		result["reconciled"] = reconcile_party(connection, row)
	ledger = ledger_of(connection, channel["id"])
	result.update(owed=str(owed), ledger=str(ledger))
	if ledger == owed:
		if not dry_run:
			resolve_flags(connection, channel["id"])
		return result
	result["status"] = "mismatch"
	if dry_run:
		return result
	result["caught_up"], missing = catch_up(connection, client, channel)
	result.update(missing)
	ledger = ledger_of(connection, channel["id"])
	result["ledger"] = str(ledger)
	if ledger == owed:
		result["status"] = "caught up"
		resolve_flags(connection, channel["id"])
		return result
	if raise_attention:
		result["raised"] = flag_mismatch(connection, channel, owed, ledger, missing)
	return result


def resolve_flags(connection, channel_id: str):
	"""The two sides agree again: close the earlier "difference" entries of this channel."""
	for name in frappe.get_all(
		EVENT_LOG,
		filters={
			"connection": connection.name,
			"event_type": CHECK_TYPE,
			"entity_id": channel_id,
			"status": "Failed",
		},
		pluck="name",
	):
		frappe.db.set_value(
			EVENT_LOG,
			name,
			{
				"status": "Done",
				"message": _("Resolved: ERPNext and Koomyo agree again on {0}.").format(nowdate()),
			},
		)


def catch_up(connection, client, channel: dict) -> tuple[int, dict]:
	"""Read the channel's payouts and paid orders again and book what is missing (every step is safe to repeat)."""
	from koomyo_erpnext.sync.sales import book_order

	booked = 0
	problems = {"orders_failed": [], "payouts_failed": []}
	try:
		payouts = list(client.iter_list(f"/sales-channels/{channel['id']}/payouts", {}))
		orders = list(client.iter_list("/orders", {"sales_channel_id": channel["id"], "limit": 100}))
	except KoomyoAPIError as e:
		problems["payouts_failed"].append(str(e)[:200])
		return booked, problems
	for payout in reversed(payouts):  # oldest first
		frappe.db.savepoint("koomyo_channel_catch_up")
		try:
			if payout.get("status") == "voided" and not find_existing(
				"Payment Entry", connection, payout["id"]
			):
				continue
			before = find_existing("Payment Entry", connection, payout["id"])
			sync_payout(payout, connection, client)
			booked += 0 if before else 1
		except Skipped:
			continue
		except Exception as e:
			frappe.db.rollback(save_point="koomyo_channel_catch_up")
			problems["payouts_failed"].append(f"{payout.get('reference') or payout['id']}: {_text(e)}")
	for order in orders:
		if (
			find_existing("Sales Invoice", connection, order["id"])
			or (order.get("payment") or {}).get("status") != "paid"
		):
			continue
		frappe.db.savepoint("koomyo_channel_catch_up")
		try:
			book_order(order, connection)
			booked += 1
		except Skipped:
			continue
		except Exception as e:
			frappe.db.rollback(save_point="koomyo_channel_catch_up")
			problems["orders_failed"].append(f"{order.get('number')}: {_text(e)}")
	if connection.sync_refunds:
		booked += catch_up_refunds(connection, client, orders, problems)
	return booked, problems


def catch_up_refunds(connection, client, orders: list[dict], problems: dict) -> int:
	"""Refunds of the channel's orders that no event brought (after the orders, oldest first)."""
	from koomyo_erpnext.sync.refunds import book_refund

	booked = 0
	for order in orders:
		if money((order.get("amounts") or {}).get("amount_refunded") or "0", "amount_refunded") <= 0:
			continue
		if not find_existing("Sales Invoice", connection, order["id"], include_cancelled=False):
			continue
		try:
			refunds = list(client.iter_list("/refunds", {"order_id": order["id"], "limit": 100}))
		except KoomyoAPIError as e:
			problems["orders_failed"].append(f"{order.get('number')}: {_text(e)}")
			continue
		for refund in sorted(refunds, key=lambda r: r.get("created_at") or ""):
			if find_existing("Sales Invoice", connection, refund["id"]):
				continue
			frappe.db.savepoint("koomyo_channel_catch_up")
			try:
				book_refund(refund, connection, client=client)
				booked += 1
			except Skipped:
				continue
			except Exception as e:
				frappe.db.rollback(save_point="koomyo_channel_catch_up")
				problems["orders_failed"].append(f"{order.get('number')} refund: {_text(e)}")
	return booked


def _text(error) -> str:
	return frappe.utils.strip_html(str(error))[:200]


def flag_mismatch(connection, channel: dict, owed: Decimal, ledger: Decimal, problems: dict) -> bool:
	"""One entry per channel per day that needs attention: ERPNext and Koomyo disagree about what the partner owes."""
	event_id = f"channel-check:{channel['id']}:{nowdate()}"
	existing = frappe.db.get_value(EVENT_LOG, {"connection": connection.name, "event_id": event_id})
	listed = "; ".join(problems.get("orders_failed", []) + problems.get("payouts_failed", []))
	message = _(
		"Channel {0}: Koomyo says the partner owes {1}, ERPNext's receivable shows {2}. After reading the channel's orders and payouts again the difference is still {3}. {4}"
	).format(
		channel.get("name") or channel.get("code"),
		owed,
		ledger,
		owed - ledger,
		(_("Could not be booked: {0}.").format(listed) if listed else "")
		+ " "
		+ _("Look at the channel's orders and payouts in Koomyo, then press Retry."),
	)
	if existing:
		# Already raised today: keep one entry, but let it say the latest reason.
		frappe.db.set_value(EVENT_LOG, existing, {"status": "Failed", "message": message[:1000]})
		return False
	log = frappe.new_doc(EVENT_LOG)
	log.update(
		{
			"connection": connection.name,
			"company": connection.company,
			"event_id": event_id,
			"event_type": CHECK_TYPE,
			"seq": 0,
			"received_via": "ERPNext",
			"event_created": now_datetime(),
			"entity_type": "Sales Channel",
			"entity_id": channel["id"],
			"payload": json.dumps({"type": CHECK_TYPE, "data": {"id": channel["id"]}}, indent=1),
			"status": "Failed",
			"error_class": "Fatal",
			"message": message[:1000],
			"attempts": 0,
			"processed_on": now_datetime(),
		}
	)
	log.insert(ignore_permissions=True)
	return True


@handles(CHECK_TYPE, switch="sync_sales", scopes=SCOPES)
def on_check_retry(event: dict, connection, client: KoomyoClient | None = None):
	"""Retry of a flagged mismatch: catch up and check that channel again."""
	channel_id = (event.get("data") or {}).get("id")
	client = client or KoomyoClient.for_connection(connection)
	channel = client.get("/sales-channels/" + channel_id)
	result = check_channel(connection, client, channel, dry_run=False, raise_attention=False)
	if result["status"] in ("mismatch", "unreadable"):
		raise Fatal(
			_("Channel {0}: Koomyo says {1}, ERPNext shows {2}. Still different.").format(
				channel.get("name"), result.get("owed"), result.get("ledger")
			)
		)
	return None
