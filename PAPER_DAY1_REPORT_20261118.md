# First day of b36103c — saved decisions and paper execution

Primary window:17Nov2026 14:44:44.868–18Nov14:44:44.868 Asia/Baku. Supplemental18Nov16:02 and risk snapshot16:32:42–16:37:16 are shown separately. Runtime remained b36103c/PID548045, exact module hash and ordinary profile confirmed. Later confirmed fixes are maintenance, not retroactive changes to these results.

[Reproducible evidence](research/PAPER_DAY1_20261118.json); offline calculator `research/paper_day1_audit_20261118.py`. Raw1.6MB bundle kept on D:/Documents/Codex/stream-work/paper-day1-raw.json with SHA in evidence. Saved data only, no late REST prices/model retries. Original28ledger events match the installation/balance-audit prefix exactly.

## All scheduled opportunities

| Baku cycle | start | saved model reply | candidates L/S | decision | actual outcome |
|---|---|---|---|---|---|
|11-17 16:02|17.11 16:02:05.728|17.11 16:02:33.636|4/4|WAIT|WAIT: cross-timeframe disagreement and intervening levels, as explicitly saved in the reply|
|11-17 20:02|17.11 20:02:05.733|17.11 20:02:33.027|4/4|SHORT|No order: limit_would_take_now at17.11 20:02:33.523|
|11-18 00:02|18.11 00:02:01.130|18.11 00:02:26.437|4/4|SHORT|EXPIRED unfilled 18.11 00:15:00.005|
|11-18 04:02|18.11 04:02:05.762|18.11 04:02:32.011|4/4|SHORT|EXPIRED unfilled 18.11 04:15:00.125|
|11-18 08:02|18.11 08:02:05.768|18.11 08:02:24.588|4/4|SHORT|EXPIRED unfilled 18.11 08:15:00.058|
|11-18 12:02|18.11 12:02:01.163|18.11 12:02:42.515|4/4|LONG|LONG filled; TIME close 18.11 13:32:46.746|
|11-18 16:02 supplemental|18.11 16:02:00.068|18.11 16:02:53.822|4/4|SHORT|EXPIRED unfilled 18.11 16:15:00.226|

Six primary opportunities all ran once:1WAIT,4SHORT,1LONG. Supplemental16:02 is another SHORT. Across7opportunities:7unique attempts/results/usage/ingests, all completed, network_retry=false, zero pre-model skips/empty candidate lists, zero uncertain model outcomes. Each snapshot contains4L+4S, not56independent trading signals.

One SHORT was rejected AFTER a valid model response. The saved immutable20:44local archive preserves analyst_last REJECTED/limit_would_take_now, linked by the exact response ID. No order/reserve was created; this is the passive-entry guard, not failed delivery or a technical cancellation. The execution-time bid causing that rejection was not retained; no later quote is substituted. Model WAIT is explicitly linked to the saved explanation above, not assigned a guessed motivation.

## Accepted orders and price eligibility

| cycle | side | limit | SL | TP | saved quotes | max saved ask | outcome |
|---|---|---:|---:|---:|---:|---:|---|
|11-18 00:02|SHORT|63871|64099.1|61984.7|6183|63787.00|EXPIRED/no fill|
|11-18 04:02|SHORT|65136.4|65272.9|64283.5|6127|64976.30|EXPIRED/no fill|
|11-18 08:02|SHORT|64636|64787.5|63658.1|6149|64485.00|EXPIRED/no fill|
|11-18 12:02|LONG|64766.4|64478.8|66074.2|19972|64810.00|TIME/CLOSED|
|11-18 16:02|SHORT|64257.6|64592.9|61971.3|3274|64230.00|EXPIRED/no fill|

Primary:4accepted orders =3expired SHORT+1filled/closed LONG. Supplemental:1SHORT expired. Four EXPIRED cancellations each have exact saved causes=[EXPIRED], paused=false, blocked=null and full reserve release; zero technical order cancellations. There was no partial fill. Recorded SHORT quote maxima stay below the limit. These are observed quote bounds, not a complete raw trade tape or a maker fill probability; the exact cancellation reason is expiry without modeled execution. No hypothetical later trade is assigned.

## One new completed trade — not the historical00:27SHORT

Plan `d8abc95be214f24ad85f24e14b73c9f9f1e6ff3e32764aececa56dcfb3af29cf`.
LONG0.001BTC, entry18Nov12:02:46.492 at64766.4, exit13:32:46.746 at64578.5568, reasonTIME. Entry source trade3530685388 has price64765.10, buyer_maker=true, quantity0.117, source time13ms AFTER arming and receipt12:02:46.894: no pre-arm backfill. Price/quantity are paper-model fills, not real exchange orders.

| gross USDT | entry fee | exit fee | funding | net |
|---:|---:|---:|---:|---:|
|-0.1878432|0.0323832|0.0322892784|0|-0.2525156784|

TIME deadline13:32:46.492; saved trigger and fill13:32:46.746,254ms later. Full fresh quote confirmed, age746ms; saved bid64643.20 *0.999 =64578.5568. Modeled impact0.0646432 is already inside gross. Funding buffer was returned after coverage; it is not funding income. Position/accounting CLOSED13:32:50.000. Observed holding5400254ms; no open remainder/reserve/margin/buffer.

Exact release-period reconciliation:

```text
1000.0321471104653898110 - 0.1878432 - 0.0646724784 + 0
=999.7796314320653898110 USDT
```
All12new events balance individually, old28 unchanged, current40events/96postings/15plans/8fills/1funding reconcile. Only this LONG is a post-release closed trade. The historical SHORT48b56f...eda48 loss predates installation and is excluded. No unrealized/open PnL at the check; no position crosses the24h boundary.

## Data/protection failures — preserved, not declared solved

11saved DATA_GAP:1idle bootstrap17Nov14:44:57.332 before ready,3later idle gaps (17Nov18:15:42.732;18Nov11:27:50.741 and12:01:26.109),7with the LONG open:

18.11 12:49:26.461, 18.11 12:59:18.344, 18.11 13:00:10.337, 18.11 13:05:01.540, 18.11 13:05:07.290, 18.11 13:05:25.306, 18.11 13:29:47.533.

All later gaps have contiguous IDs, no socket fault/overflow/reconnect, with stale delivered events and local dequeue delay. They are not REST1000-page saturation; largest lifetime queue highwater2731/32768, initial connection counter1 unchanged. This does not identify CPU/SQLite/backup as the cause. No entry remainder existed during the7open-risk gaps; there is no ledger cancellation or protective exit at those times. No stale events were converted to retrospective fills.

The LONG has three saved quote-unconfirmed/recovery intervals:12:19:10.681–10.844 (163ms),13:04:57.038–13:05:01.286 (4248ms),13:16:18.527–18.661 (134ms). The first/third coincide with the confirmed clock race below; the4248ms interval is not explained by it. Stored confirmed quote bounds during the position: bid64500–64809.90, mark64504.06620290–64810; SL64478.8/TP66074.2 were not reached in those stored quotes. Missing/unconfirmed periods cannot establish whether an unobserved crossing occurred or a hypothetical PnL. At TIME deadline fresh components existed and the saved detection delay is254ms; do not infer earlier SL/TP guarantees.

Four retained false book_receipt_time examples are a specific confirmed implementation defect, not a causal explanation of all gaps. Validation time was read BEFORE locking; a receiver published1–6ms later, with valid past source E/T, then appeared future relative to the old timestamp. Deterministic baseline fails; the minimal clock-inside-lock fix passes3new+23existing targeted tests. Genuine source/receipt clock regression still rejects; strict5000ms unchanged. Two retained examples occurred with LONG open. Fix/installation results are recorded below as post-audit maintenance.

## AI expenses — separate from virtual trading

| scope | calls | input/output tokens | estimated USD | conservative reserved USD |
|---|---:|---:|---:|---:|
|first_24h|6|23419/8141|0.0064124|0.016752|
|supplement|1|3881/1365|0.0010706|0.00279|
|all|7|27300/9506|0.007483|0.019542|

Lifetime original-journal reservations0.094700USD; remaining original0.25USD cap0.155300USD. Reservation and estimated usage are different quantities, NOT two expenses to add. These are the saved configured-token-price estimates, not a provider invoice or authenticated owner tariff. VM/storage monetary charges were not available in the saved evidence and were not estimated. No extra AI request was made for this audit/maintenance.

## Conclusion and finite next question

Production path now demonstrably handled a real model-selected paper LONG entry, independent TIME management, full close, fees/funding finalization and reconciliation;4SHORT orders naturally expired, one selected SHORT failed the correct marketability gate,1WAIT had candidates. The main observed trade-frequency impediment is passive-entry eligibility, not absent scheduled calls or hidden LONG-only routing. No strategy/threshold/risk change is justified by one losing trade. No profitability inference and no wait for ten trades.

A concrete remaining engineering question is backup headroom: current archive required315s of the newly bounded360s. A future LIMITED experiment, not started here, could compare fixed gzip levels9and1 on the SAME completed73.5MB SQLite snapshot at existing15%CPU/96MiB, without GCS upload/model/trades. Success: decompress SHA/ledger exactly equal, under96MiB and <=180s; record compressed bytes as the transfer-cost tradeoff, not free savings. No parameter grid, new infrastructure or automatic follow-up. This is not a trading-profit experiment.

## Confirmed maintenance after the report snapshot

[Fix protocol](PAPER_DAY1_FIX_PROTOCOL_20261118.md), [maintenance/source hashes and receipts](research/PAPER_DAY1_MAINTENANCE_20261118.json). The temporary baseline reproduces false `book_receipt_time`; fixed3new+23existing targeted cases pass26tests in14.458s at CPU50%/128MiB. No unchanged full suite, model call or actual test trade.

Installed **6e7ff408f786bf25f78aaf91e7e6bf54ac372ba6**,18Nov17:03:36.392Baku, verified17:03:46.186, `/opt/tradingbot-ai-brackets-6e7ff40-paper`, PID592049. Only production `research/ai_limit_stream.py` changed; SHA `ce108b1f6446b4d6d4dc968d7cbcc360a311963abd2496811fa2569fa503bde3`. Clock is sampled under the component lock; no tolerance/freshness/risk/strategy change. Runtime module remains b36103c, SHA `be52d3ccbd291295bd7beaa104a0dda9d3d8e3f38b2939b2276ee5fed16f06e2`; ordinary mode without --phase-diagnostics. Old detailed phase file preserved/frozen, not a missing writer.

A separate confirmed operational defect prevented complete external bracket backups: five3min timeouts12:27–16:27 while the original AI-budget backups succeeded. Latest complete external paper ledger had been11:29,34events/6fills and did not include this LONG. At existing15%CPU/96MiB, current73,510,912B archive/restore required315.210308394s, longer than the old180s limit. Added ONLY bounded `TimeoutStartSec=6min` to existing backup service; same timer, CPU/RAM and encryption. Pre-switch download/decrypt/restore passed40events/8fills, manifest `04c902760ec9fe0da38e06990692f4382ab13efccf877199a265a937b0113b19`. Failed partial versions retained and not called backups.

Fresh post-switch external restore completed **17:14:26.437Baku**:10chunks,40events/8fills/1funding and exact same free balance; manifest `fbab084eb6ce5b8159a9a3ae5c6ca1000e4fdb119eda56765eb4019df28bac20`, SQLite `2a2ae9e65487c26f37b2dfd52694cd5c19c2e94a311f5a43c07dba153e3e6bab`. Normal startup local backup also reopened/balanced with exact matching financial tables. Encryption remains **host-bound**: download/decrypt on this authorized host does not establish recovery after loss of its host key; prior recovery limitation remains.

Pre/post financial SHA `8b6d8e334ec49f4717eddfa464e2cb5d47c078ed22a3d14e5f4918bbf47d46ae` identical:40events/96postings/15plans/8fills/1funding, no active position/remainder/reserve. Code rollback metadata `/var/lib/tradingbot-ai-budget-backup/day1-6e7ff40`; retain current financial DB/WAL/events, never roll data back to undo code. Timer coordinated only during flat switch and restored ACTIVE/enabled, next unchanged20:02Baku; analyst74f77d0 and UIc96ad52 unchanged. One financial writer/one Telegram poller; actual /ai orders1676B render verified. All four working market pauses preserved.

Live ordinary sample17:06:34.597–17:08:35.606:206publications, max interval2523ms,0quote/market/funding error publications. Sampled BBO source age373ms/mark695ms. An idle bootstrap gap17:03:42.898 precedes ready. **Final read17:16:51.125 additionally found a real idle DATA_GAP17:13:32.210**:55contiguous rows,3stale, oldest age5419ms, dequeue wait4322ms, poll interval5532ms; newest age335ms, connected/nooverflow/reconnect. Previous cycle total1246ms including finalize972ms; these saved counters do NOT fully attribute the intervening delay. Do not claim all backlog fixed, or that backup overlap proves its cause. No financial risk at that time, no retrospective fill. Current market_errors recovered to null. This empty-account sample is not fresh live-risk validation of the fix; partial-risk regression is a temporary fixture, and actual LONG TIME evidence belongs to b36103c.

Final saved state17:16:51–54Baku: free999.779631432065389811USDT, flat/balanced, no reserves; AI journal36attempts/reserved94700microUSD, unchanged by maintenance. Budget, credential/locks, risk and original schedule preserved. VM remains on. Completed one-shot `trading-bot-paper-b36103c` deleted; subsequent app delete confirmed not_found before17:19:37Baku. No replacement diagnostic chain registered. Remaining unknown backlog attribution does not justify a speculative setting change.
