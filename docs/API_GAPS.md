# Koomyo API gaps and decisions

This file lists what the Koomyo public API and its docs (docs.koomyo.com, `openapi.json` v1.0.0, read 2026-10-26) do not give yet, and the choices the app makes where the docs leave room. Each entry says what is missing, why it matters, what the app does about it, and what would fix it.

Status keys: **Open** (waiting on Koomyo), **Decision** (the app chose; the owner should confirm), **Blocked** (that part is not built), **Resolved**.

---

## 1. No signal that a feed position is past the 30-day retention (Open)

**What.** `GET /events?after=<seq>` returns whatever is left above `after`. Nothing in the answer says whether events right after `<seq>` were already deleted after 30 days.

**Why.** A connection that has been down for more than 30 days would otherwise skip deleted events silently.

**Workaround.** The app records when it last read the feed to the end (`last_caught_up_on`). If that was more than 29 days ago, it stops reading, marks the connection **Needs Re-sync**, sends an alert, and offers **Restart Feed** plus a backfill. This is safe but cautious: it may trigger even when nothing was actually lost.

**Planned by Koomyo.** An `oldest_seq` field and a `gap` flag on the `GET /events` list answer. When they exist, replace the 29-day rule in `sync/events.py` (`FEED_SAFE_DAYS`) with: if `gap` is true, or `after < oldest_seq - 1`, the connection needs a re-sync.

## 2. Scope list in CLAUDE.md (Resolved)

CLAUDE.md now lists `suppliers.write`, `ingredients.write` and `refs.write`, matching `reference/scopes`. Each sync declares the scopes it needs, and Test Connection shows the missing ones for the syncs that are switched on.

## 3. Events feed pages by number (Resolved)

CLAUDE.md now says the feed pages by event number. The app stores `last_seq` per connection, taken only from `next_after`. List endpoints keep their opaque `next_cursor`.

## 4. A refund carries an amount, not lines (Decision)

**What.** The `Refund` schema has `amount`, `payment_method` and `reason`, but no lines. Koomyo's own ERPNext guide says a partial refund "needs a decision from your accountant".

**What the app does.**
- A refund of the whole invoice becomes a Credit Note that mirrors it exactly, using ERPNext's own `make_sales_return`.
- A partial refund follows the connection's **Partial Refunds** setting:
  - **Leave for Manual Booking** (default, the safe choice): the event fails with a message naming the invoice and amount, so a person books it by hand, or changes the setting and presses Retry.
  - **Proportional Credit Note**: every tax and charge row is reversed in the share `refund / invoice total`, rounded to the cent. The revenue line takes the rounding, so the Credit Note total equals the refund exactly. Tests check that two partial refunds add back up to the original tax.

**Wish.** `lines` (order line id, quantity, amount) on `Refund`, so a partial refund can be booked by line.

## 5. Menu items, stock on sales invoices, and card fees (Decision)

- **Menu items.** Each Koomyo menu item (or combo) becomes one non-stock ERPNext Item, made the first time it is sold, in the connection's Menu Item Group. It is matched by `(koomyo_connection, koomyo_id)`, so two restaurants never share an Item. Modifiers and combo upgrades are an extra line, so each Koomyo `line_total` is kept exactly.
- **No stock on Sales Invoices.** Koomyo already deducts recipe ingredients, so invoices never move stock (`update_stock = 0`); stock comes from the stock sync. There is no switch for this: a switch to move menu-item stock would double-count.
- **Card and wallet fees.** Koomyo does not report provider fees. Each Payment Map row can hold a fee percent and a fee account, and the fee is booked as a deduction on the Payment Entry. Leave it at 0 when fees are booked from bank statements.
- **Tax-inclusive prices.** ERPNext's "included in print rate" recomputes tax and can differ from Koomyo by a cent. The app instead adds two exact rows: the tax moved out of income, and the tax to the tax account.

## 6. Shift close: refunds and rider cash in the variance (Decision)

**What.** Koomyo's `cash_variance` is `counted_cash - expected_cash`. Its `expected_cash` does not subtract refunds paid out of the drawer, so in the docs' own example the variance is -723.50 when the drawer is really 1.00 over. The `Shift.refunds` object gives a count and an amount, but not the method. It is described as "refunds paid out in this shift" or "paid out of the drawer", depending on the page.

**What the app does.** Refunds are booked with their own payment out (slice 2). The Journal Entry therefore books `counted_cash - (expected_cash - refunds.amount)` between the drawer account and the Cash Over/Short account, and its remark shows each figure and Koomyo's own variance. The integration test replays the docs' day and checks that the cash account ends at exactly `counted - opening float`.

**Assumptions to confirm with Koomyo.**
- `refunds.amount` holds only refunds paid in cash from the drawer.
- Cash that riders hand in (`rider_cash_handed_in`) is already in the orders' payments, so it reaches the cash account through those Payment Entries.

If either is wrong, the booked over/short is off by that amount, but the remark shows the figures.

**Wish.** `refunds.cash_amount` (refunds paid from the drawer) on `Shift`, and a line in the docs on how rider cash appears in order payments.

## 7. Stock: menu-item waste, short transfers, central warehouses, valuation (Decision / Open)

- **Waste of a whole menu item (Open).** `waste.recorded` with `ingredient: null` and a `menu_item_id` has no ingredient lines, so the app cannot tell which stock left. Such an event fails with a clear message and is not booked; this includes food thrown away when a cut order is cancelled. **Wish:** the ingredient lines (`ingredient_id`, `quantity`) on `Waste`, or the matching `GET /stock-movements?reference_id=<waste id>`.
- **Transfers that arrive short (Decision).** The docs' own `stock_transfer.completed` example is `received` with `quantity_received` "0" of 5 sent. The app moves what arrived from the source to the destination. It books the rest as a Material Issue from the source to the Transfer Loss account, assuming Koomyo took the full amount out of the source when it was sent. Please confirm with Koomyo.
- **Central warehouses (Open).** Transfers can come from or go to a Koomyo "warehouse" location, but no endpoint lists warehouses (only `GET /branches`). Map them on the Branch Map with Location Type "Warehouse". An unmapped one fails with its exact id. **Wish:** `GET /warehouses`.
- **Daily usage has no id.** Koomyo says a day is sent once per branch, so the Stock Entry is keyed `usage:<branch>:<business_date>`. A day with more put back than used (net negative) gets a separate Material Receipt keyed `...:returned`.
- **Valuation.** Stock leaves at ERPNext's own valuation, as the docs suggest ("If your ERP values stock itself, use only quantity_used"). Stock comes in (adjustments, counts) at Koomyo's `unit_cost`. ERPNext's cost of goods can therefore differ from Koomyo's `cost` when the two valuations differ.
- **Not enough stock in ERPNext.** Koomyo allows negative stock and ERPNext by default does not. Such an event fails with ERPNext's message. Set the opening stock (and the Opening Stock Date), or allow negative stock in Stock Settings, then press Retry.
- **Quantities.** Koomyo sends up to 4 decimals. They are stored as ERPNext floats and may be rounded to the site's float precision (3 by default). Money is not affected.

## 8. Purchasing and customers (Decision / Open)

- **One receipt per purchase order (Open).** `POST /purchase-orders/{id}/receive` works once; a second call answers `409 already_received`. The first ERPNext Purchase Receipt can be partial and is sent as is. A second receipt for the same order fails with a clear message, and the rest must be adjusted in Koomyo. **Wish:** several receipts per order.
- **Which orders are sent.** A Purchase Order is written to Koomyo only when its Warehouse is on a connection's Branch Map. Branch rows send `branch_id`; central-warehouse rows leave it out, so the order goes to Koomyo's main warehouse. One Warehouse per order, and one line per ingredient. Only Items linked to a Koomyo ingredient can be ordered.
- **Quantities and costs** go out in the ingredient's stock unit: `stock_qty`, and `base_net_rate / conversion_factor` in company currency, up to 4 decimals.
- **Suppliers are not shared between connections.** A Supplier carries one Koomyo link. Using it for a second connection's order fails with a message to make a separate Supplier.
- **Ingredient cost.** The push is triggered by a person changing an ingredient Item's Valuation Rate, through `PATCH /ingredients/{id}`. ERPNext's moving-average valuation after receipts is not pushed automatically.
- **Koomyo-made purchase orders.** A `purchase.received` for an order created in Koomyo (no ERPNext Purchase Order) is skipped with a message. ERPNext is the source for purchasing. Supplier and ingredient events from Koomyo (`supplier.*`, `ingredient.*`) are not synced into ERPNext.
- **Customers are one way, Koomyo to ERPNext.** Only name, phone and email are copied, and the ERPNext id is stored on the Koomyo customer with `PUT /refs/customer/{id}`. ERPNext customers are not created in Koomyo, so `customers.write` is not used. Customer events arrive only when the webhook's customer data switch is on.
- **External references written.** Supplier and purchase order references go in the create call. Customer and ingredient references use `PUT /refs`. Orders, refunds and shifts are not given references, to save the rate limit; the ERPNext documents carry the Koomyo id instead.

## 9. Backfill (Decision / Blocked)

- **Manual stock adjustments are not backfilled (Blocked).** `GET /stock-movements` lists every movement, and its `reason` is "a system code such as order_deduction, order_cancel_restore or po_received, or the text a person typed". The docs name only `order_deduction`, `order_cancel_restore`, `po_received`, `transfer_out` and `transfer_in`, not the codes for waste or count movements. So a manual adjustment cannot be told apart safely from movements booked another way, and counting it twice would corrupt stock. Live `stock.adjusted` events are booked as usual. Backdated manual adjustments must be entered by hand. **Wish:** a documented list of reason codes, or a `kind=manual` filter on `GET /stock-movements`.
- **Daily usage is rebuilt from movements (Decision).** There is no list endpoint for `stock.usage_daily`. The backfill sums the `order_deduction` and `order_cancel_restore` movements per branch, day and ingredient (net, as the event is) and books them under the live event's own key, `usage:<branch>:<day>`. So a day that came live is "already there", and a day brought by backfill is not doubled when the live event arrives. Days not yet settled are left to the live event; a day counts as settled 2 hours after the restaurant's midnight. **Wish:** `GET /stock-usage?business_date=` returning the same object as the event.
- **Which orders.** Orders with `payment.status = paid` on each business day (`GET /orders?business_date=&payment_status=paid`). A paid order cancelled later is still a sale, and its refund comes in through the refund list.
- **Lists without a day filter.** Refunds, shifts, waste, counts and transfers are read with a wider UTC window (`created_after`, `opened_after`, `sent_after`) and picked by `business_date`. Customers (`updated_since` = From Date) and received purchase orders (no date filter, picked by `business_date`) are one part each.
- **Closed periods.** The run is refused, naming the reason, when the range touches any of: an Accounting Period closed for a document type it would post; a submitted Period Closing Voucher ending on or after From Date; the Accounts Settings freeze date; or, for stock and purchases, the Stock Settings freeze date.
- **Pacing.** Each call checks `X-RateLimit-Remaining`. When fewer than a tenth of the key's allowance is left, the run waits for it to refill. A 429 is waited out (`Retry-After` plus one second) and the same page is asked for again. The position (Koomyo's opaque `next_cursor` per list) is saved after every page, so Stop, a crash or a restart carries on from there.

## 10. Complaints (Blocked)

**What.** CLAUDE.md lists an optional complaint sync (Koomyo complaints become ERPNext Issues, scope `complaints.read`). The Koomyo public API v1 has no complaints endpoint, no `complaint.*` event and no `complaints.read` scope; the OpenAPI file and the docs do not mention complaints at all.

**What the app does.** Nothing. Following CLAUDE.md rule 1 ("If a sync is not built yet, its switch does not exist"), there is no complaints switch. The Issue doctype already carries the `koomyo_connection` / `koomyo_id` fields, so the sync can be added without a migration of existing data.

**Wish.** `GET /complaints` (with `branch_id`, `created_after`, `status`, cursor paging), `complaint.created` / `complaint.updated` events, and a `complaints.read` scope. Each complaint would need an `id`, `branch_id`, `order_id`, `customer` (id only without customer data), `category`, `description`, `status`, `created_at` and `business_date`.

## 11. Not verified

- Nothing has run against a real Koomyo or a Koomyo sandbox yet. Every Koomyo answer in the tests comes from a fake server built from the docs examples.
- Frappe and ERPNext v16: the code avoids version-specific APIs (`koomyo_erpnext/compat.py`), but it has only run on v15 (Frappe 15.120.1, ERPNext 15.118.0). The Koomyo workspace JSON uses the v15 format.

## 12. Per-order stock deduction (Decision / Open)

**What the app does.** With Stock Deduction set to Per Order, each `order.paid` queues a stock step. The step reads `GET /stock-movements?reason=order_deduction` for the order's branch, in a window from 5 minutes before the order was created to 6 hours after it was paid, and keeps the rows whose `reference_id` is the order. They become one Material Issue (key `order:<id>`). `order.cancelled` does the same with `order_cancel_restore` and makes a Material Receipt (key `order-restore:<id>`). Daily usage is skipped on per-order days. A change of setting applies from the next day, so a day is never booked both ways.

**Assumptions to confirm with Koomyo.**
- `reference_id` on an `order_deduction` or `order_cancel_restore` movement is the order id; the schema says "the order ... behind the movement".
- An order's deductions are recorded within that window. They are made when lines go to the kitchen, before payment.

**Wish.** A `reference_id` filter on `GET /stock-movements`, so one call returns exactly one order's movements; or the deducted ingredient lines on `order.paid` itself.

## 13. Sizes, modifiers and categories (Decision / Open)

- **Sizes are Item Variants.** A menu item with sizes is a template Item, keyed by the Koomyo menu item id. Each size is a variant on the "Koomyo Size" attribute, keyed by the Koomyo `size_id`, with its own price on the selling price list (ERPNext does not price templates). An order line with a `size_id` books on that variant. A size dropped in Koomyo is disabled, never deleted. An Item that already has invoices or stock as a plain Item cannot become a template in ERPNext, so it keeps taking all its sizes.
- **Modifiers and combo choices are not separate Items.** `list_price`, `unit_price` and `line_total` on an order line already include the chosen options (docs guide: 1099 = 1000 + 99), so the line is one invoice row and the old "extras" row is gone (see item 15). Every modifier, free ones included, is written in the line's description. Their ingredients reach stock through Koomyo's own deductions.
- **Categories are Item Groups.** One leaf group per Koomyo category, named "<category> (<connection>)", under "<connection> Menu". Items without a category go to the connection's Menu Item Group. A category renamed in Koomyo gets a new group on the next load, its items move there, and the old group is left for you to remove.

## 14. Orders made in Koomyo (Decision / Open)

With Book Orders Made in Koomyo on, a `purchase.received` for an order with no ERPNext Purchase Order creates a Supplier (if new), a submitted Purchase Order (ordered quantities at Koomyo's unit cost) and a Purchase Receipt (received quantities), all stamped with the Koomyo ids. Orders made in ERPNext are never doubled, and Koomyo-made orders and suppliers are never sent back (suppliers carry `origin:koomyo` in `koomyo_branch`). A Koomyo supplier whose name already exists in ERPNext is created as "<name> (<connection>)", never merged. Orders without a branch are received into the connection's Main Warehouse.

- Koomyo tells us about an order only when it is **received**, so ERPNext sees it then, not when it was drafted or sent.
- Costs can carry extra decimals; ERPNext rounds each rate and line to its currency precision, so a total can differ from Koomyo's by a few paisa.
- **Wish:** `purchase_order.created` events, so the order exists in ERPNext before it arrives.

## 15. Discounts on sales (Decision / Open)

Source: docs.koomyo.com/guides/discounts and the OpenAPI schemas `OrderLine`, `Order`.

- Each line is posted at `price_list_rate = list_price`, discount per unit `list_price - rate`, `rate = net_price / quantity`. "Enable Discount Accounting for Selling" is switched on at install and by a patch; with it off a discounted order fails with a message saying so, instead of posting a wrong book.
- Each source (`promotion`, `voucher`, `manual`, `points_reward`, `slice_reward`, `gift_item`) has an account in the connection's **Discount Map**; an unmapped source is `Failed: NeedsMapping` naming the row. ERPNext takes one discount account per line (and one for the order), so a line or order that mixes sources is posted on its largest part and a Journal Entry (`koomyo_id` = `discount:<order id>`) moves the other parts to their own accounts. A full refund reverses that entry (`discount-refund:<refund id>`). A **partial** refund (Proportional Credit Note) is not split by source: the credit note matches the refund to the cent (tested), but the reclassification entry is not reversed in part. Wish: a refund that says which discount it gives back.
- Tax is Koomyo's `tax` as sent, never recalculated. Quantity above 1 with a net that does not divide to the cent becomes two rows (the last unit takes the odd cent), so the invoice still equals `net_price` exactly.
- A gift item stored before Koomyo kept prices has null `list_price`: it is posted at its net price with no discount and a note on the row, nothing is guessed.
- If a line's `discounts` parts do not add up to `list_total - net_price`, the whole discount goes to the largest part's account and a note is added to the invoice remarks.
- The identity `items_list_total - discount_total = items_subtotal - discount - voucher_discount` is checked when the order carries both totals; a break is Fatal and nothing is booked.
- Channel `api` orders are ordinary sales. Stock is not moved by the invoice (Koomyo deducts ingredients itself).
- **Not verified against a live order with a discount** (the only live connection has none). Verified on the local test site with Koomyo's documented numbers (GL entries asserted).

