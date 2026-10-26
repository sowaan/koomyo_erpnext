# Koomyo API gaps and doc questions

Things this app needs that the Koomyo public API or its docs (docs.koomyo.com, `openapi.json` v1.0.0, read 2026-10-26) do not give yet. Each entry says what, why, and what would fix it. The app works around each one as described.

## 1. No signal that a feed position is older than the 30-day retention

**What.** `GET /events?after=<seq>` documents what happens on an empty page (`next_after` = the `after` sent), but not what happens when the events right after `<seq>` have already been deleted after 30 days. Nothing in the answer tells a client that it skipped deleted events.

**Why.** CLAUDE.md requires the app to stop, alert and offer a guided re-sync when its saved position is older than the feed keeps, rather than guess.

**Workaround.** The app records when it last read the feed to the end (`last_caught_up_on`). If that was more than 29 days ago, it stops reading, marks the connection **Needs Re-sync**, sends an alert, and offers **Restart Feed** plus a backfill. This is safe but cautious: it can trigger even when no event was actually lost.

**Wish.** Either an `oldest_seq` field on the `GET /events` list answer, or a `410 feed_position_expired` error when `after` is below the oldest kept event.

## 2. CLAUDE.md scope list differs from the docs

**What.** The docs (`reference/scopes`) split writing into `suppliers.write`, `purchasing.write`, `ingredients.write`, `customers.write` and `refs.write`. CLAUDE.md lists only `purchasing.write` and `customers.write`.

**Why.** Supplier write-back, ingredient cost push and external references (slice 5) need the three extra scopes.

**Plan.** Each sync declares the scopes it needs, taken from the docs. Test Connection shows exactly which ones are missing. CLAUDE.md should be updated to match the docs.

## 3. Events feed pages by number, not by cursor

**What.** CLAUDE.md says "cursors are opaque". That holds for every list endpoint (`next_cursor`), but `GET /events` pages by the numeric `seq` (`after` / `next_after`), as `events/feed` and the OpenAPI `EventList` schema state.

**Plan.** The app follows the docs. The feed position is the integer `last_seq`, read only from `next_after` and never computed by the app. List cursors are stored and passed back untouched.
