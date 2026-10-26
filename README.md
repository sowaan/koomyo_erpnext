## Koomyo ERPNext

Sync a Koomyo restaurant with ERPNext, automatically: sales, refunds, till cash, stock and purchasing.

Koomyo is a restaurant POS. This app is the ERPNext side of its public integration API ([docs.koomyo.com](https://docs.koomyo.com)). One ERPNext site can hold several Companies, and each Company can be linked to its own Koomyo restaurant through its own **Koomyo Connection**.

| Koomyo | ERPNext |
|---|---|
| Paid order | Submitted Sales Invoice, plus one Payment Entry per payment |
| Refund | Credit Note against the invoice, plus the payment out |
| Shift closed | Journal Entry for the cash over or short |
| Daily ingredient usage, waste, adjustments, stock counts | Stock Entries and Stock Reconciliations |
| Stock transfer | Material Transfer between Warehouses |
| Customer | Customer (name, phone and email only) |
| ← Purchase Order, Purchase Receipt, Supplier, ingredient cost | Written to Koomyo from ERPNext |

### Versions

| Frappe / ERPNext | Status |
|---|---|
| v15 | Supported and tested (Frappe 15.120, ERPNext 15.118). |
| v16 | Supported on the same branch, not yet run on a v16 bench. |

### Install

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench --site your.site install-app koomyo_erpnext
bench --site your.site migrate
```

The app needs ERPNext. Background workers and the scheduler must run (`bench start` in development, supervisor in production), because all syncing happens in background jobs.

## Setup guide

### 1. In Koomyo: an API key

In **Settings > Integrations > API keys**, create one key for this ERPNext connection. Give it the scopes of the syncs you will switch on:

| Sync | Scopes |
|---|---|
| Always | `events.read`, `catalog.read` |
| Sales, Refunds | `orders.read` |
| Shifts | `shifts.read` |
| Stock | `stock.read`, `refs.write` |
| Purchasing | `purchasing.read`, `purchasing.write`, `suppliers.write`, `ingredients.write` |
| Customers | `customers.read`, `refs.write` |

The key starts with `kyo_live_` and is shown once. If ERPNext has a fixed public address, you can add it to the key's IP allowlist.

### 2. In ERPNext: a connection

Open **Koomyo Connection > New** (or the **Koomyo** workspace) and fill in:
- **Connection Name**: any name, such as the brand.
- **Company**: every document from this restaurant is booked here. It must use the restaurant's currency.
- **API Key**: the key from step 1.
- **Start From Date**: the events feed is read from this day. Earlier data comes in with a backfill.
- **External Reference Name**: leave it as `erpnext`, unless two connections share one Koomyo restaurant; then give each its own name.

Save, then press **Test Connection**. It shows:
- the restaurant, its currency and time zone;
- the key's scopes, and any that are missing;
- the restaurant's branches, with a warning for those not yet mapped.

Press **Add Unmapped Branches** to copy them into the Branch Map.

### 3. Maps

Every map row must belong to the connection's Company; this is checked when you save and again when posting. A missing row is never guessed. The event fails with a message naming the row to add, and **Retry** books it once the row exists.

- **Branch Map**: each Koomyo branch to a Cost Center and a Warehouse. Koomyo central warehouses, which appear in stock transfers, use Location Type **Warehouse**. Their id appears in the failed event's message.
- **Payment Map**: each Koomyo payment method code (`cash`, `card`, ...) to a Mode of Payment and the cash or bank account it lands in. **Fee %** and **Fee Account** book a card or wallet provider's fee as a deduction on each Payment Entry.
- **Tax Map**: Koomyo sends one tax amount and rate per order. Map each rate (or leave the rate empty for any rate) to a tax account. A row for one branch wins over a row for all branches.
- **UOM Map** (stock): each Koomyo unit (`kg`, `pcs`, ...) to an ERPNext UOM.
- **Waste Map** (stock): each waste reason (`spoilage`, `staff_meal`, `comp`, `tasting`, `other`) to an expense account.

### 4. Switch the syncs on

| Switch | What it books | Settings it needs |
|---|---|---|
| **Sync Sales** | `order.paid` becomes a Sales Invoice (no stock movement) plus Payment Entries. `order.cancelled` adds a note to the invoice. | Walk-in Customer, Menu Item Group, Income Account, Selling Price List. Delivery Fee, Channel Markup and Payment Method Discount accounts once orders use them. |
| **Sync Refunds** | `refund.created` becomes a Credit Note plus the payment out. A full refund mirrors the invoice. A partial refund follows **Partial Refunds**. | Partial Refunds |
| **Sync Shifts** | `shift.closed` becomes a Journal Entry for the cash over or short. | Cash Over/Short Account (Cash Drawer Account only when ambiguous) |
| **Sync Stock** | Daily usage, waste, adjustments, counts and transfers become stock documents. | Opening Stock Date, Ingredient Item Group, Cost of Goods Sold and Stock Adjustment accounts, UOM Map and Waste Map. Transfer Loss Account once a transfer arrives short. |
| **Sync Purchasing** | Purchase Orders and Purchase Receipts for a mapped Warehouse go to Koomyo, with their Supplier; ingredient cost changes go to Koomyo; an order received in Koomyo becomes a Purchase Receipt. | Ingredients must be Items linked to Koomyo, which the stock sync creates. |
| **Book Orders Made in Koomyo** (under Sync Purchasing) | An order made in the Koomyo admin and received there becomes a Supplier (if new), a Purchase Order and a Purchase Receipt. | Main Warehouse, Supplier Group |
| **Sync Customers** | Koomyo customers become Customers. | Customer Group, Territory, and the webhook's customer data switch on in Koomyo. |

Then tick **Enabled**.

**Partial refunds.** Koomyo sends a refund amount without lines. The default, **Leave for Manual Booking**, fails such a refund with a clear message, so an accountant decides. **Proportional Credit Note** reverses revenue and each tax and charge row in the same share, exact to the cent.

**Stock Deduction.** This setting picks how sales take ingredients out of stock:
- **Daily (from Koomyo usage)**, the default: one Material Issue per branch per day, after the day ends.
- **Per Order (from Koomyo movements)**: each paid order takes out exactly what Koomyo deducted for it, and a cancelled order puts it back.

The stock step is its own Event Log row, so a stock problem never holds up the invoice. A change of setting applies from the next day, so no day is counted twice.

**Menu, sizes, modifiers.** **Load Menu and Ingredients** makes every Koomyo menu item an Item, in an Item Group per Koomyo category. A menu item with sizes becomes a template, with one variant per size, each with its own price. Ingredients become stock Items. Sales book on the size's variant. Modifiers (extra cheese, no onion) are written on the invoice line, and paid extras are an extra line on the same Item.

**Opening Stock Date.** Stock events dated before this day are skipped, because the Warehouses' opening stock already contains them. Set it to the day after you entered opening stock.

**Dry Run.** With Dry Run ticked, every event is checked against the maps and ERPNext's rules, but nothing is saved and nothing is written to Koomyo. The Event Log says what would have been made. Untick it and press **Feed > Book Dry-Run Events** to book them for real.

### 5. In Koomyo: the webhook

On the connection, open **Webhook > Copy Webhook Address**. In Koomyo, go to **Settings > Integrations > Webhooks > Add endpoint**:
- paste the address;
- pick the events in **Events to Register** on the connection;
- keep customer data off, unless Sync Customers is on.

Koomyo shows a `whsec_` secret once. Paste it into the connection's **Webhook Secret** and save. Press **Send test** in Koomyo; a Done `webhook.test` row appears in the Event Log.

The address must be public https. On a laptop, use a tunnel such as ngrok.

### How it stays complete

- **Webhook.** Each event is checked (signature with this connection's secret, timestamp within 5 minutes, the right restaurant), stored once, and answered at once. Booking happens in a background job, one queue per connection.
- **Catch-up.** Every 5 minutes, each connection reads Koomyo's events feed from the last event number it read. Anything a webhook missed is booked then, so an hour of downtime loses nothing, and nothing doubles.
- **Feed gap.** Koomyo keeps events for 30 days. A connection that has not read the feed for 29 days stops, shows **Needs Re-sync** and alerts. Bring the gap in with a backfill, then press **Feed > Restart Feed**.
- **Duplicates.** Every document made from Koomyo carries the Koomyo id, and the database refuses a second one per connection.

### Event Log

**Koomyo Event Log** has every event: received via webhook, feed, backfill, or from ERPNext (writes to Koomyo). Each row shows its status, error, attempts, the linked document and the payload, with secrets and private customer fields removed.
- **Failed: Retryable** (for example, Koomyo down) is retried on its own, with growing waits.
- **Failed: NeedsMapping** waits for the map row its message names, then **Retry**.
- **Failed: Fatal** needs a person; the message says what to do.

Use **Retry** on a row, or **Retry All Failed** on the list. Done rows are deleted after the days set in **Koomyo Settings**.

### Backfill: past dates

**Koomyo Backfill > New**: pick the connection, the dates and what to bring, and save. Then:
1. **Preview** counts what it would find, create, find already there, skip and fail, and shows a sample. It writes nothing.
2. **Start** brings it in. Documents get their own Koomyo dates, and anything already in ERPNext, from live sync or an earlier run, is left alone.
3. **Stop** pauses; **Start** carries on from where it was.
4. **Retry Failed** tries the failed records again; **Re-run a Day** runs one day again.

A run is refused if the range touches a closed accounting period, a Period Closing Voucher, frozen accounts or frozen stock, or (for stock) days before the Opening Stock Date. Calls are paced to the key's rate limit. **All Connections** on the list makes one backfill per connection.

### Dashboard

**Koomyo Dashboard** (in the Koomyo workspace) shows each connection's health:
- its last event and how far the feed has been read;
- what was booked, skipped or failed in the last 24 hours, and what is waiting;
- events that need a map row, and running backfills.

It has **Retry All Failed** and **Catch Up Now** buttons, and refreshes every 30 seconds.

### Several Companies and permissions

Add one connection per Company. Each has its own key, secret, webhook address, maps, switches, feed position, Event Log and backfills. A connection that is down or rate-limited never slows the others.
- Two connections may point at the same Koomyo restaurant only for different Companies, with different branches.
- Give accountants the **Koomyo Manager** role plus a User Permission on their Company. They then see and retry only that Company's connection, events, backfills and dashboard card.

### Alerts

Set **Alert Email** in **Koomyo Settings**. A connection with a revoked key, an unreachable server or a feed gap sends one email a day until it is fixed.

### Troubleshooting

| You see | Do this |
|---|---|
| Test Connection: "Koomyo does not accept this API key" | The key was revoked or mistyped. Make a new key and paste it. |
| Webhook test answers 401 | The Webhook Secret is wrong or old (after New secret in Koomyo), or the server clock is off by more than 5 minutes. |
| Failed: NeedsMapping | Add the row the message names, then Retry. |
| "ERPNext refused it: ... insufficient stock" | Koomyo allows negative stock, ERPNext does not by default. Enter opening stock (and the Opening Stock Date), or allow negative stock, then Retry. |
| Needs Re-sync | The feed was not read for 29 days. Backfill the gap, then Restart Feed. |

What the Koomyo API does not provide yet, and the choices made where the docs leave room, are in [docs/API_GAPS.md](docs/API_GAPS.md).

## Try it against a Koomyo sandbox

Use a sandbox restaurant and a test ERPNext site. Never use production for this.

1. Create the API key with every scope in the table above, and note the sandbox API address, such as `https://<sandbox-host>/api/public/v1`.
2. Expose the test site over https (for example `ngrok http 8000`), and set its `host_name` to that address: `bench --site test_site set-config host_name https://<your-tunnel>`.
3. Make a connection (step 2), set **Dry Run** on, fill the maps, switch on the syncs, and press Test Connection.
4. Add the webhook (step 5) and press Send test.
5. In the sandbox, sell a split-payment order with a discount, refund part of it, close the shift, record waste, a stock count and a transfer. Each event appears in the Event Log as a dry run with what it would make.
6. Untick Dry Run, press **Book Dry-Run Events**, and check the Sales Invoice, Payment Entries, Credit Note, Journal Entry and Stock Entries against the sandbox figures.
7. Make a Purchase Order in ERPNext for a mapped Warehouse, and check that it appears in Koomyo. Receive it in ERPNext, and check that Koomyo's stock moved.
8. Stop the workers for an hour while selling, then start them. The catch-up books what came in, once.
9. Make a backfill for a past week: Preview, then Start, then Start the same range again and check that it creates nothing.

## Development

```bash
bench --site test_site set-config allow_tests true
bench --site test_site run-tests --app koomyo_erpnext
```

The tests use a fake Koomyo built from the docs examples in `koomyo_erpnext/tests/fixtures/`; they never call a real Koomyo or send email. Linting uses `pre-commit` (ruff, prettier, eslint):

```bash
cd apps/koomyo_erpnext && pre-commit install
```

### License

mit
