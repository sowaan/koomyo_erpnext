## Koomyo ERPNext

Sync your Koomyo restaurant sales, stock and purchasing with ERPNext, automatically.

Koomyo is a restaurant POS. This app is the ERPNext side of its public integration API ([docs.koomyo.com](https://docs.koomyo.com)). One ERPNext site can hold several Companies, each linked to its own Koomyo restaurant through a **Koomyo Connection**.

### Versions

| Frappe / ERPNext | Status |
|---|---|
| v15 | Supported and tested (developed on Frappe 15.120 / ERPNext 15.118). |
| v16 | Supported on the same branch. Not yet tested on a v16 bench. |

One branch serves both versions; `koomyo_erpnext/compat.py` holds the few differences.

### What works today (slice 1: the foundation)

- **Koomyo Connection** (one per Company): API address, API key and webhook secret (both stored encrypted), start-from date, branch map.
  - **Test Connection** calls `GET /account` and `GET /branches`. It shows the restaurant, its currency and time zone, the key's scopes and any that are missing, flags branches that are not mapped yet, and warns when the server clock is off.
  - Refuses a Company whose currency differs from the restaurant's.
  - Refuses a second connection for the same restaurant and Company, and a branch mapped into two Companies.
- **Webhook receiver** at `/api/method/koomyo_erpnext.api.webhook?c=<token>`. The token is random per connection.
  - Checks the HMAC signature with that connection's secret, refuses timestamps more than 5 minutes off, and checks the event belongs to that restaurant.
  - Stores the event once (duplicates change nothing) and answers at once; the work runs in a background job.
- **Catch-up job** every 5 minutes, one job per connection. It reads `GET /events` from the last event number and stores events through the same duplicate guard as the webhook. If it has not read the feed for 29 days it stops, marks the connection **Needs Re-sync** and offers **Restart Feed**.
- **Koomyo Event Log**: every event with its status (Pending, Processing, Done, Skipped, Failed), error type, attempts, payload (secrets and private customer fields removed) and linked document.
  - Retry and Retry All Failed buttons.
  - Automatic retries with back-off for passing failures.
  - Nightly clean-up of old Done rows.
- **Alerts**: a connection with a revoked key or a feed gap shows **Needs Attention** or **Needs Re-sync** and emails the alert address once a day.
- **Koomyo Manager** role. Combine it with a User Permission on Company so an accountant sees only their Company's connection and events.

**Sales and refunds (slice 2).** With **Sync Sales** on, each paid Koomyo order becomes a submitted Sales Invoice plus one Payment Entry per payment, matching Koomyo's `amount_due` to the cent. With **Sync Refunds** on, each refund becomes a Credit Note and the payment out. Partial refunds follow the **Partial Refunds** setting (see `docs/API_GAPS.md`). **Dry Run** checks everything and saves nothing.

**Shifts and cash (slice 3).** With **Sync Shifts** on, each closed till shift books its cash over or short as one Journal Entry, between the drawer's cash account and the Cash Over/Short account. The amount is the counted cash against Koomyo's expected cash less the refunds paid from the drawer, which are already booked with the refunds.

**Stock and transfers (slice 4).** With **Sync Stock** on, each event becomes a stock document in the mapped Warehouses:
- the daily ingredient usage becomes a Material Issue to Cost of Goods Sold;
- waste becomes a Material Issue to the expense account of its reason (Waste Map);
- manual adjustments become a Material Receipt or Issue;
- stock counts become a Stock Reconciliation;
- transfers become a Material Transfer, with any shortfall booked as a loss.

Ingredients become stock Items, with units mapped through the UOM Map. Nothing dated before the **Opening Stock Date** is booked, so opening stock is never counted twice.

**Purchasing and customers (slice 5).** With **Sync Purchasing** on:
- a submitted Purchase Order for a mapped Warehouse is written to Koomyo, with its Supplier;
- its Purchase Receipt is sent as Koomyo's receipt (one per order);
- a changed ingredient Valuation Rate updates Koomyo's ingredient cost;
- an order received in the Koomyo admin becomes a Purchase Receipt.

Every write carries an `Idempotency-Key` and appears in the Event Log as received via ERPNext. With **Sync Customers** on, Koomyo customers become Customers (name, phone and email only).

**Backfill (slice 6).** **Koomyo Backfill** brings in any past date range through the same handlers, mapping and duplicate guard as live sync.
- It reads Koomyo's list endpoints, not the 30-day feed, and posts each document with its own Koomyo date.
- **Preview** counts and checks without writing anything; **Start** runs it.
- **Stop** and **Retry Failed** do what they say; **Re-run a Day** runs one day again.
- A run is refused when the range touches a closed accounting period or a frozen stock date.
- Running the same range twice changes nothing.
- **All Connections** (list view) makes one backfill per connection.

### Install

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench --site your.site install-app koomyo_erpnext
```

### Set up a connection

1. In Koomyo, go to **Settings > Integrations > API keys** and create a key with at least `events.read` and `catalog.read`. Test Connection lists any other scope a switched-on sync needs.
2. In ERPNext, open **Koomyo Connection > New**. Enter a name, the Company, the API key and a start-from date, then save.
3. Press **Test Connection**, then **Add Unmapped Branches** and pick a Cost Center and Warehouse for each branch.
4. Copy the **Webhook Address** into Koomyo (**Settings > Integrations > Webhooks**, Add endpoint, customer data off). Paste the `whsec_` secret it shows into the connection, save, and tick **Enabled**.
5. Press **Send test** in Koomyo. A Done `webhook.test` row appears in the Event Log.

### Tests

```bash
bench --site test_site set-config allow_tests true
bench --site test_site run-tests --app koomyo_erpnext
```

The tests use a fake Koomyo server built from the docs examples in `koomyo_erpnext/tests/fixtures/`. They never call a real Koomyo.

### Contributing

This app uses `pre-commit` (ruff, eslint, prettier):

```bash
cd apps/koomyo_erpnext
pre-commit install
```

### License

mit
