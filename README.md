## Koomyo ERPNext

Sync a Koomyo restaurant with ERPNext, automatically: sales and discounts, refunds, till cash, stock, purchasing, suppliers and complaints.

Koomyo is a restaurant POS. This app is the ERPNext side of its public integration API ([docs.koomyo.com](https://docs.koomyo.com)). One ERPNext site can hold several Companies, and each Company can be linked to its own Koomyo restaurant through its own **Koomyo Connection**.

| Koomyo | ERPNext |
|---|---|
| Paid order | Submitted Sales Invoice (each line at its list price with its discount shown, per discount source), plus one Payment Entry per payment |
| Refund | Credit Note against the invoice (the discount it gives back reversed per source), plus the payment out |
| Shift closed | Journal Entry for the cash over or short |
| Daily ingredient usage, waste, adjustments, stock counts | Stock Entries and Stock Reconciliations |
| Stock transfer | Material Transfers between Warehouses, in steps through a Transit Warehouse |
| Customer | Customer (name, phone and email only) |
| Purchase order (created, updated, cancelled, received) | Purchase Order and one Purchase Receipt per delivery, with tax and landed cost |
| Supplier card | Supplier, Address, Contact and Payment Terms Template |
| Purchase request (branches ask, purchasing approves) | Material Request, once lines are approved |
| Complaint | Issue (no customer name or phone) |
| ← Purchase Order (one share per branch request line), Purchase Receipt (one delivery per place), Supplier, ingredient cost, the Material Request's name | Written to Koomyo from ERPNext |

### Versions

| Frappe / ERPNext | Status |
|---|---|
| v15 | Supported and tested (Frappe 15.120, ERPNext 15.118). |
| v16 | Supported on the same branch and tested (Frappe and ERPNext `version-16`, Python 3.14, Node 24): the whole suite passes on a v16 site. A new v16 site needs its language set (System Settings) before sales can book, (completing the Frappe setup wizard, or setting it in System Settings). |

### Install

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/sowaan/koomyo_erpnext --branch develop
bench --site your.site install-app koomyo_erpnext
bench --site your.site migrate
```

The app needs ERPNext. Background workers and the scheduler must run (`bench start` in development, supervisor in production), because all syncing happens in background jobs.

## Quick start: the Setup Wizard

Open the **Koomyo** workspace and press **Setup Wizard** (or the button on any connection). A guided, full-screen setup walks through seven steps:

1. **Welcome**: pick the Company and name the connection.
2. **Connect**: paste the API key and test it. It shows the restaurant, currency, time zone and the key's scopes.
3. **Set Up**: the branches, central warehouses, payment methods, units, tax, discount and waste accounts and defaults are read from Koomyo and matched to your ERPNext records. You review every line (use an existing record, create one, or skip) and apply. Nothing is saved before you press Apply, filled settings are never changed, and **Undo Set Up** on the connection removes what was created if nothing uses it.
4. **What to Sync**: switch on sales, refunds, shifts, stock, purchasing, customers and complaints.
5. **Menu**: optionally load the menu and ingredients as Items.
6. **Webhook**: copy the address and events for Koomyo and paste the secret.
7. **Finish**: start in Dry Run (recommended) or live.

The connection form has the same settings in six tabs (Connection, Setup, Syncs, Webhook, Menu and Ingredients, Status), with a "Still to do" checklist on top. A banner on the desk offers the wizard while the site has no connection.

## Setup guide (by hand)

### 1. In Koomyo: an API key

In **Settings > Integrations > API keys**, create one key for this ERPNext connection. Give it the scopes of the syncs you will switch on:

| Sync | Scopes |
|---|---|
| Always | `events.read`, `catalog.read` |
| Sales, Refunds | `orders.read` |
| Shifts | `shifts.read` |
| Stock | `stock.read` (also warehouses and daily usage), `refs.write` |
| Purchasing | `purchasing.read`, `purchasing.write`, `suppliers.write`, `ingredients.write` |
| Purchase requests | `purchasing.read` (requests and orders), `purchasing.write` (orders with shares, receive), `refs.write` (links); no new scope. Use the "ERP sync" key preset in Koomyo. |
| Customers | `customers.read`, `refs.write` |
| Complaints | `complaints.read` |

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

- **Branch Map**: each Koomyo branch to a Cost Center and a Warehouse. Koomyo central warehouses, which appear in stock transfers, use Location Type **Warehouse**. Set Up From Koomyo and the wizard read Koomyo's warehouse list (`GET /warehouses`, scope `stock.read`) and map every active warehouse; Koomyo's main warehouse becomes the connection's Main Warehouse. (If a Koomyo does not have the endpoint, the warehouses that appear in stock transfers are used instead.) A purchase order for a warehouse goes to the Warehouse mapped here.
- **Payment Map**: each Koomyo payment method code (`cash`, `card`, ...) to a Mode of Payment and the cash or bank account it lands in. **Fee %** and **Fee Account** book a card or wallet provider's fee as a deduction on each Payment Entry.
- **Tax Map**: Koomyo sends one tax amount and rate per order. Map each rate (or leave the rate empty for any rate) to a tax account. A row for one branch wins over a row for all branches.
- **Discount Map** (sales): each Koomyo discount source (`promotion`, `voucher`, `manual`, `points_reward`, `slice_reward`, `gift_item`) to an account. Every sale line is posted at its list price with the discount shown on the line and booked to its source's account (Selling Settings > Enable Discount Accounting for Selling is switched on at install). A line that mixes sources is posted on its largest source and a small Journal Entry moves the rest, so each source's account holds exactly Koomyo's figure. A full refund reverses that entry too.
- **UOM Map** (stock): each Koomyo unit (`kg`, `pcs`, ...) to an ERPNext UOM.
- **Waste Map** (stock): each waste reason (`spoilage`, `staff_meal`, `comp`, `tasting`, `other`) to an expense account.

**Purchase taxes and charges** (Syncs tab, Purchasing): Koomyo orders carry tax, withholding and extra charges. Recoverable tax goes to the **Purchase Tax Account**; tax that cannot be claimed back, and charges spread over cost, go to the **Landed Cost Account** (type Expenses Included In Valuation) and so into the cost of the goods; withholding is a deduction on the **Withholding Tax Account**, never part of cost; charges that are not spread go to the **Charges Expense Account**. **Charges in Landed Cost** follows Koomyo's setting on each charge, or forces None, By Value or By Quantity for the connection. A charge spread by quantity is posted with a Landed Cost Voucher on each delivery (ERPNext's own tax rows only spread by value). Each delivery takes its share of every row by what has arrived so far and the last delivery takes the remainder, so the rows add up to Koomyo's figures.

**Stock transfers in steps.** With a **Transit Warehouse** on the connection (Syncs tab, Stock), a transfer is posted as Koomyo moves it: `transfer.sent` is a Material Transfer with Transit on from the source into the Transit Warehouse (ERPNext's Send to Warehouse); each delivery (`transfer.received`, keyed by its receipt id) is the matching Receive at Warehouse of that delivery to the destination; stock returned to the sender is a receive back into the source; stock lost in transit is a Material Issue from transit to the **Transfer Loss Account**, with Koomyo's reason and value in the remarks; `transfer.cancelled` cancels the send (or fails with a message if a delivery was booked against it). Every event carries the transfer as it stands, so they can arrive in any order, twice, or be replaced by `stock_transfer.completed` or a backfill, and nothing is doubled. A draft creates nothing. Without a Transit Warehouse the step events are skipped with a message, and `stock_transfer.completed` books a transfer in one step as before. In ERPNext v15 the steps are `add_to_transit` and `outgoing_stock_entry` on Material Transfer; v16 is expected to behave the same (not run).

**Suppliers.** With Book Orders Made in Koomyo on, `supplier.created` and `supplier.updated` bring Koomyo's whole supplier card into ERPNext: the Supplier (name, Tax ID from `tax_number`, website, default currency, disabled when not active, payment terms as a shared Payment Terms Template: cash on delivery, payment in advance, or net N days), one Billing Address (when the card has a first address line, a city and a country ERPNext knows; otherwise it says what is missing and makes none), one Contact (name, phones, WhatsApp, email), and a plain "Koomyo card" text on the Supplier for what ERPNext has no field for (code, second tax number and status, credit limit, lead time, minimum order, delivery days, tags, preferred, rating, notes). Bank details are not in Koomyo's API. A supplier made in ERPNext stays ERPNext's: Koomyo's echo of it changes nothing, and a Koomyo supplier whose name already exists in ERPNext is made as "<name> (<connection>)", never merged. A purchase order makes its supplier from the whole card (`GET /suppliers/{id}`) when it can be read.

**Purchase requests (Koomyo) and consolidated orders (ERPNext).** A branch asks for ingredients in Koomyo; purchasing approves, rejects or changes quantities there (staff only: the API has no call for it, so ERPNext reads the decisions); ERPNext orders.
- **Material Request.** Each submitted request becomes one Material Request (type Purchase) when purchasing has approved lines: only lines whose own status is approved (or later), at `approved_quantity`, never the quantity the branch asked for, for the branch's Warehouse. A change of what is approved rebuilds it while nothing was ordered from it; after that the event fails with a message for a person. A request rejected or cancelled in Koomyo cancels it (or fails the same way if ERPNext already ordered from it). Its name is written back to Koomyo (`refs`), so Koomyo can find it.
- **Order it.** Make a Purchase Order from one or several branches' Material Requests (one supplier, one row per request line; rows of one Item need one rate). When Sync Purchasing is on, it is sent with one share per request line (`allocations`), summed into one line per ingredient. **Deliver Shares To** says where a share goes when it names no place: Koomyo Decides, Warehouse (to be distributed to the branches by Koomyo transfers) or Branches (straight to each branch). Koomyo checks everything in one transaction with the admin's own rules, and a refusal creates nothing: a line that is not approved, more than purchasing approved (if Koomyo's own staff also order, whoever orders a line first wins), or a restaurant without the feature. The event then fails with the reason and **Retry** sends it again once it is fixed. If staff edit or cancel the order in Koomyo, it comes back as `purchase.updated` or `purchase.cancelled` and the quantity returns to the request.
- **Receiving per place.** Each Purchase Receipt of such an order goes to Koomyo as one delivery per destination (the Warehouse is looked up on the Branch Map: a branch row, or Location Type Warehouse), with the line's total at that place so far; a receipt that spans two places is two deliveries. A Purchase Order closed in ERPNext after part arrived is finished short at every place still waiting. Deliveries Koomyo receives become one Purchase Receipt each, into the Warehouse of the place they landed at (a share for the warehouse lands in its Warehouse, a branch share in the branch's). Goods held in a warehouse reach the branches by Koomyo transfers that staff make (Distribute to branches); they arrive as `transfer.sent` and `transfer.received` and become Material Transfers through the Transit Warehouse, as above.
- **ERP mode: tell the restaurant.** In Koomyo, **Settings > Inventory and costs > Purchase requests > Who orders what the branches ask for** must be the external system, so only ERPNext orders (Koomyo then hides and refuses its own Create orders). The setting is not readable through the API, so the app cannot check it. And an order ERPNext makes is a **draft in Koomyo: do not press Send on it there**, because Send emails the supplier and ERPNext already placed the order.
- Test Connection calls `GET /purchase-requests?limit=1` when Sync Purchase Requests is on, and says plainly if the restaurant lacks the Inventory app or the purchase requests feature (403 `feature_not_available` or `app_not_enabled`).

**Supplies and low stock.** Koomyo's ingredients now have a kind: food, packaging or **supply** (soap, gloves, bin liners, uniforms). Supplies are an expense and never part of food cost: what an employee takes (`supply_issue`) is booked from stock to the **Supplies Expense Account** (Stock section), not to Cost of Goods Sold or Stock Adjustment. They use the same purchase orders, receiving, transfers and counts, and the Item keeps its kind. `stock.low` (an ingredient at a place fell to its low stock level) is shown in the Event Log and books nothing. An open stock count that staff delete never reaches ERPNext (nothing had moved stock); a closed one cannot be deleted.

**Payment methods.** Koomyo can add payment method codes at any time (for example an online card). A method that is not in the Payment Map fails with the row to add, unless the connection has a **Default Mode of Payment** (with a default account for the Company): then the sale or refund is booked to it, the Payment Entry says so, and the Event Log row shows a warning to add a Payment Map row.

### 4. Switch the syncs on

| Switch | What it books | Settings it needs |
|---|---|---|
| **Sync Sales** | `order.paid` becomes a Sales Invoice (no stock movement) plus Payment Entries. `order.cancelled` adds a note to the invoice. | Walk-in Customer, Menu Item Group, Income Account, Selling Price List. Delivery Fee, Channel Markup and Payment Method Discount accounts once orders use them. |
| **Sync Refunds** | `refund.created` becomes a Credit Note plus the payment out. A full refund mirrors the invoice. A partial refund follows **Partial Refunds**. | Partial Refunds |
| **Sync Shifts** | `shift.closed` becomes a Journal Entry for the cash over or short. | Cash Over/Short Account (Cash Drawer Account only when ambiguous) |
| **Sync Stock** | Daily usage, waste (from its ingredient lines), adjustments, counts and transfers become stock documents. | Opening Stock Date, Ingredient Item Group, Cost of Goods Sold and Stock Adjustment accounts, UOM Map and Waste Map. Transfer Loss Account once stock is lost in transit. Transit Warehouse to post transfers in steps (see below). |
| **Sync Purchasing** | Purchase Orders and Purchase Receipts for a mapped Warehouse go to Koomyo, with their Supplier. Each Purchase Receipt is one delivery sent with the line totals so far, with its delivery note, so an order can arrive in several; a Purchase Order closed in ERPNext after part arrived is finished short in Koomyo; ingredient cost changes go to Koomyo; an order received in Koomyo becomes a Purchase Receipt. | Ingredients must be Items linked to Koomyo, which the stock sync creates. |
| **Book Orders Made in Koomyo** (under Sync Purchasing) | An order made in the Koomyo admin becomes a Supplier (if new) and a submitted Purchase Order once it is **ordered** (a draft creates nothing); each delivery becomes its own Purchase Receipt; an edit before any delivery rebuilds the order; a cancel cancels it (or, if goods were already received, fails with a message for a person). The safety net `GET /purchase-orders?updated_since=` runs every 5 minutes through the same code. | Main Warehouse, Supplier Group; for orders with tax or charges also the Purchase Taxes and Charges accounts below |
| **Sync Customers** | Koomyo customers become Customers. | Customer Group, Territory, and the webhook's customer data switch on in Koomyo. |
| **Sync Purchase Requests** (under Sync Purchasing) | Koomyo purchase requests become Material Requests once purchasing has approved lines, at the approved quantity, for the branch's Warehouse. A Purchase Order made in ERPNext from those Material Requests is sent to Koomyo with a share for each request line, and each Purchase Receipt is received at its own place. See Purchase requests below. | Branch Map rows for the branches, **Deliver Shares To**; the restaurant needs Koomyo's Inventory app and purchase requests feature. |
| **Sync Complaints** | Koomyo complaints (`complaint.created`, `complaint.updated`) become Issues: ticket number and summary, status, priority, category, order number, resolution and its cost. The customer's name and phone are never copied. | The key's `complaints.read` scope and a restaurant with the Complaints app. |

Then tick **Enabled**.

**Refunds.** A refund of everything collected (the order's only refund) mirrors the invoice, and Koomyo's `lines` for it are checked against the invoice's lines. Any other refund is an amount without lines, and the app never guesses which lines. The default, **Leave for Manual Booking**, fails such a partial refund with a clear message, so an accountant decides. **Proportional Credit Note** reverses revenue and each tax and charge row in the same share, exact to the cent. When the refund carries `discount_given_back` (its share of the order's discount, by source), the credit note is built at the price before the discount and a small Journal Entry puts each source's part on its own account, so the discount accounts end up exactly right after any number of partial refunds.

**Till cash.** The shift's over or short is `counted cash - (expected cash - refunds paid back in cash from the drawer)`. Koomyo's expected cash is opening cash plus the shift's cash payments plus rider cash handed in at that till, and it does not subtract refunds; only the cash part (`refunds.cash_amount`) leaves the drawer, card refunds do not.

**Stock Deduction.** This setting picks how sales take ingredients out of stock:
- **Daily (from Koomyo usage)**, the default: one Material Issue per branch per day, after the day ends.
- **Per Order (from Koomyo movements)**: each paid order takes out exactly what Koomyo deducted for it (read by order id), and a cancelled order puts it back.

The stock step is its own Event Log row, so a stock problem never holds up the invoice. A change of setting applies from the next day, so no day is counted twice.

**Menu, sizes, modifiers.** **Load Menu and Ingredients** makes every Koomyo menu item an Item, in an Item Group per Koomyo category. A menu item with sizes becomes a template, with one variant per size, each with its own price. Ingredients become stock Items. Sales book on the size's variant. Modifiers (extra cheese, no onion) are written on the invoice line and their price is already inside the line's list price. Koomyo's option groups (what each option takes from stock) are read for costing only: the app works out the stock a line takes (item recipe for the size plus each chosen option, a swap giving the replaced ingredient back), but Koomyo deducts the stock itself and nothing is moved from it.

**Opening Stock Date.** Stock events dated before this day are skipped, because the Warehouses' opening stock already contains them. Set it to the day after you entered opening stock.

**Koomyo Access.** **Read and Write** (the default) also sends what is made in ERPNext: suppliers, Purchase Orders and receipts, ingredient costs, and the ERPNext id on Koomyo's records. **Read Only** only ever reads from Koomyo. Nothing is written back, the key doesn't need the write scopes, and the ERPNext-to-Koomyo purchasing doesn't run. Sales, refunds, shifts, stock, customers and orders made in Koomyo still come into ERPNext as usual. Work queued for Koomyo is skipped and can be sent later with Retry after switching back.

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
- **Feed gap.** Koomyo keeps events for 30 days and says so in every answer of `GET /events` (`oldest_seq`, `latest_seq`, `gap`). When `gap` is true the connection stops reading the feed, shows **Needs Re-sync**, alerts, and makes and starts a **Backfill** of the days since its last complete read (sales, refunds, shifts, stock, customers and purchases, as far as each is switched on). When that backfill completes without failures the feed continues from `latest_seq` by itself. If it cannot start (a closed accounting period) or ends with failures, the connection says what to do: fix it and press **Start** or **Retry Failed** on that backfill. **Feed > Restart Feed** remains for starting again from today.
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

Ticking **Purchases** also brings in every Koomyo supplier card (needs Book Orders Made in Koomyo). Stock backfill reads Koomyo's daily usage (`GET /stock-usage`; a day Koomyo has not settled yet is skipped with a message and can be run again later), waste, counts, transfers and hand adjustments, all under the same keys as live events. A run is refused if the range touches a closed accounting period, a Period Closing Voucher, frozen accounts or frozen stock, or (for stock) days before the Opening Stock Date. Calls are paced to the key's rate limit. **All Connections** on the list makes one backfill per connection.

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
| Needs Re-sync | Koomyo reported a feed gap. The re-sync backfill is running or needs you (see Feed gap above). |
| Failed: "Discount Map" or "Discount Accounting" | Add the Discount Map row for that source, or switch on Selling Settings > Enable Discount Accounting for Selling, then Retry. |
| A transfer or purchase order fails naming a Koomyo warehouse id | Run Set Up From Koomyo again (it reads the warehouse list), or add a Branch Map row of Location Type Warehouse. |
| Failed: "Koomyo will not take Purchase Order ..." | A request line is not approved, or the order takes more than purchasing approved, or the restaurant lacks the feature: the message says which. Nothing was created in Koomyo; fix it and Retry. |
| Failed: "Purchase Order ... was cancelled in ERPNext, but Koomyo's API cannot cancel an order" | Koomyo's API has no call to cancel a purchase order. Cancel it in the Koomyo admin (Purchases); its cancellation then comes back by itself. |
| Failed: "purchase order ... already fully received or closed short" | Koomyo has nothing more to receive for that order; adjust the stock in Koomyo. |

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

The tests use a fake Koomyo built from the docs examples in `koomyo_erpnext/tests/fixtures/`; they never call a real Koomyo or send email. The suite (332 tests) passes on v15 and on v16 (`bench init --frappe-branch version-16 --python python3.14`, Node 24, then `bench get-app erpnext --branch version-16`). Linting uses `pre-commit` (ruff, prettier, eslint):

```bash
cd apps/koomyo_erpnext && pre-commit install
```

### License

mit
