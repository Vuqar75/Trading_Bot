# Recovery of the public aggregate-trade cursor

Base: published `405d458`, including installed cancellation diagnostics and the
idle tick storage fix. This patch is prepared in the cloud checkout; it has NOT
been installed or observed on the VM here.

## Confirmed code defects and change

Previously Poller called `trade_gap()` and immediately cleared `market_error`,
allowing the next paid analyst opportunity to treat that batch as healthy.
A fromId cursor could also keep requesting the start of an old backlog (1000-row
limit per call). Existing ledger evidence proves TRADE_GAP at 16:02:40 Baku,
but cannot distinguish missing IDs, old rows, future timestamps or latency in
that particular incident. This patch does not claim to reconstruct its cause.

* Any gap leaves `public_trade_stream_unconfirmed`. An empty stream becomes
  unconfirmed once its last confirmed trade exceeds the existing 5-second limit.
  Transport/validation failure invalidates prior confirmation. A subsequent fresh,
  contiguous trade batch is required to recover; an empty response cannot erase
  a failure. Existing analyst admission consumes this market error before snapshot
  collection, reservation or model HTTP.
* After normal cancellation, only if `Book.active()` is None, perform at most one
  additional public aggTrades bootstrap request without fromId in that cycle.
  Move the cursor forward to the returned latest ID. Discard ALL bootstrap
  events: no backfilled fills, TP or paper trades. A fresh following incremental
  batch must confirm continuity. A missing/regressing bootstrap cursor fails
  closed, preserving the prior persisted cursor. This is a new public snapshot,
  not a retry of any model request. No host fallback or access-denial bypass.
* If an entry is partially filled, normal gap cancellation removes only its
  remaining entry. No cursor reseed while any plan remains active/reconciling.
  Fresh quote protection runs first and continues; subsequent valid incremental
  trades remain eligible for the existing reduce-only TP path.
* Each detected gap now durably records requested/returned IDs, up to eight
  discontinuity pairs, stale/future row counts, age extrema, request duration,
  receipt time and SHA-256 of the public batch. Latest bounded feed diagnostics
  also appear in `orders.json.trade_feeds`. Raw public response bodies are not
  archived; a hash alone cannot reconstruct them. No secrets are logged.

No schema migration, financial policy, TP/SL, 5-second freshness limit, size,
budget cap, timer, credential, historic ledger or old-market pause is changed.
Existing JSON metadata accommodates the additional fields. Old runtime can read
this database if rollback is needed. Cancellation evidence remains immutable.

## Tests

```
python -m unittest tests.test_ai_limit_trade_recovery tests.test_ai_bracket_cancellation tests.test_ai_limit_brackets tests.test_ai_limit_runtime tests.test_ai_bracket_external_backup -q
```

43 tests passed, including eight new source/financial admission tests using
injected public transports and temporary databases only. These verify idle
reseed/confirmation, active partial-risk protection, stale-event cancellation
without historical fills, sequence-vs-age evidence, empty-stream expiry,
pre-model admission, transport failure and cursor regression. Existing tests cover
transaction replay, backup restore and cancellation history. No public exchange
HTTP, model request, budget journal access or real/Testnet order was used here.

## One installation pass through the authorized local operator

1. Fetch this branch, read current HANDOFF and compare the installed runtime to
   base `405d458` and `AI_BRACKET_TRADE_RECOVERY_PATCH.json`. Preserve newer VM
   fixes. Only three runtime files are changed: ai_limit_feed.py,
   ai_limit_runtime.py and ai_limit_book.py. Do not replace the whole Telegram or
   trading application; no UI install is required.
2. Read fresh actual paper status, units and ledger. Do not assume the last
   reported flat state is current. If any plan/position/reserve/reconciliation
   is active, keep its protective service running; finish this small deployment
   at the next flat state, without a forced close. Briefly coordinate the
   analyst timer during the switch so no new request/plan races the maintenance;
   preserve and restore its prior enabled/active state, schedule, locks and budget.
   Do not create an extra analysis opportunity or replay a missed one.
3. Create a fresh consistent private ledger backup and verify its isolated
   restore, ledger balance, plans/fills and reserve. Retain old release for
   rollback. Recheck absence of risk immediately before the brief paper-service
   restart. Install the three verified files as a coherent release using the
   established deployment procedure and ownership/permissions.
4. Verify the eight new tests plus the targeted suite on the exact candidate,
   startup, then three ordinary paper cycles. Check trade_feeds and market_errors:
   recovery must show a later fresh contiguous batch; do not manufacture gaps,
   suppress errors, loosen limits or create a financial test trade. Persistent
   bad source data should remain blocked and be reported with its new evidence.
5. Verify /ai orders snapshot, unchanged historical IDs/ledger/old-market pauses,
   a single poller and restored analyst schedule. Create and externally restore
   the fresh ledger backup. Record actual commit/hashes, source status and any
   remaining blocker in HANDOFF, commit and normal push.

The cloud author has no authorized VM connection. A local green test run or
published commit does not confirm deployment or delivery on Telegram. Do not
repeat a paid AI request to demonstrate the patch. Do not disable protective
service while risk exists. Roll back runtime only, preserving financial events;
never restore a pre-deployment financial database over newer events.
