# BTCUSDT paper Spot: pilot preparation — 2026-11-07

## Installed result — 2026-11-07

The earlier sections below describe preparation, not the current status. The
paper application installed on the VM is `8900dd255a3c71322e84fcb5fd0425a99bf11580`,
working directory `/opt/trading-bot-spotpilot-8900dd2`. Its source tree matches
the full Linux-tested application tree, with a separately tested recovery-tool
hardening and two additional recovery test files. The active strategy is
`btc-trend1-48-24-v1`, **experimental paper; no economic advantage proven**.
It started 2026-11-07 06:00:13.592 UTC / 10:00:13.592 Baku from cash and
does not act on the already completed 06:00 signal. Its next eligible execution
calculation is 07:15 UTC / 11:15 Baku (07:00 UTC signal, then one 15m wait).
Spot entries were enabled after the check and reconciliation; isolated, cross
and margin remain paused. No initial capital was added. A manual pause remains
authoritative, including on restart. `/status spot`, `/report` and `/signal`
display pilot rules, limits and separate results. `/ai` says the owner stopped
paid scheduling. `/donchian` remains the independent observation.

The owner-authorized **CHECK ONLY** paper cycle was completed by the normal
single-writer `prepare -> claim -> PaperExchange -> apply` path. No real or
Testnet order was submitted. BUY `rte2ceac73997a43e6a175dafa33a2a6a9` at
05:59:24.174 UTC: 0.00013000 BTC at 76072.027005 USDT, fee
0.00988936351065 USDT. SELL `rtbb6cc23da2c84d3fa3e01ad4adf7b0c1` at
05:59:28.008 UTC: the same BTC quantity at 75995.992995 USDT, fee
0.00987947908935 USDT. Net is -0.0296532639 USDT; cash changed from
999.9991603288 to 999.9695070649 USDT. The position is closed; the cycle is
not counted in pilot entries or pilot PnL. Test state and its original request
are preserved; never re-arm a second check from them.

Real installed unit files were rehearsed on nine copies with systemd-analyze,
injected partial switching, restoration and refusal of unsafe rollback. The
application tree passed 1448 Linux tests; the recovery-only change and two
added test files passed 32 more targeted tests. The exact final candidate was
switched with the guarded coordinator and coordinated backup. Three healthy
cycles preceded release. `/var/lib/tradingbot/spotpilot-updates/20261107c/`
contains before, stopped, post-release and **post-pilot** coordinated copies;
the post-pilot financial, queue and observation copies were hash-checked,
SQLite-integrity-checked and separately restored with a balanced ledger.
The live health verifier confirmed one poller and healthy relay. No second
poller or paid AI request was made. Actual Telegram delivery of a later screen
is a user-facing observation, not inferred from rendering it as `tradingbot`.

The hourly decision window is five seconds after the 15-minute delay; public
data are fetched concurrently and execution requires a newer exchange trade
timestamp after the durable decision. A measured WSL public-data probe took
about 3.7 seconds through the fresh quote; that observation is not a service
latency guarantee. A missed or stale window does not cause a backdated trade.
The protective stop remains a paper model and gaps can exceed the threshold.
Position monitoring and confirmed manual close continue while entries pause.

This is a preparation record, not evidence that the pilot or a check trade was
started. The owner permits exactly one labelled paper-only BUY/SELL check and
the subsequent bounded pilot; neither permission enables real/Testnet orders.

## Existing system and owner decision

The installed Spot code is `13fc8f641a576262da5087ccb772b0be66c1c594`.
Its configured worker uses `DonchianSpotApplication` and `PaperExchange`.
The `PaperExchange.submit` implementation returns the local client ID and does
not send an exchange order; public prices and filters are obtained separately.
The regular paid AI timer was disabled on 2026-11-07 at 05:00 UTC while its
service was inactive. The journal, reservations, credential and backups were
not changed. The existing `/ai` public report was changed only to state the
owner's pause; the original source report and a pre-change public copy remain
preserved. Neither a model request nor a second poller was used.

At the 2026-11-07 00:00 UTC daily boundary the persisted Donchian action was
HOLD for the 2026-11-06 candle, with Spot unpaused and no order emitted. A
later public re-fetch independently reconstructs close 75,844 USDT versus
prior 60-day maximum close 86,620 USDT. This re-fetch is not a preserved
contemporaneous input and cannot independently prove that no later correction
occurred. There were zero Spot orders in the 24-hour query at approximately
05:00 UTC. The previously stored `last_action` does not preserve every past
daily input, so no additional intraday decisions should be invented.

At the initial inventory Spot cash was 999.9991603288 USDT, position quantity
zero, no unsettled order or pending unified job, and no blocked/error state.
These are observations at that time, not a pre-trade authorization: recheck
immediately before any action. A public filter probe found a candidate quantity
0.00013000 BTC, approximately 9.8761 USDT with buy impact, and a valid sell
filter at the probed price; this is not a guarantee for a later quote.

## Fixed pilot candidate

The existing research rule `trend1` in `ADAPTIVE_RESEARCH_PLAN.md` uses 1-hour
UTC closes, entry strictly above the maximum of the preceding 48 hourly closes,
exit on a close below the preceding 24-hour minimum, a 2% protective threshold
and a 168-hour maximum hold. The saved simulation uses one complete 15-minute
bar of waiting after the signal before execution. On each side it assumed 10
bps fees and 5 bps impact in the base case. No 4% take-profit or daily
Donchian entry may be inherited. The stop is a model and not a guaranteed
execution price. Historical BTC long results closed 96 trades in 2024, 95 in
2025 and 77 in the available 2026 interval under base costs; these were
development data and all three periods were net negative. The recorded
15-minute maximum entry delay refers to the fixed modelled signal-to-execution
delay, not the maximum time between signals.

The requested live pilot still needs durable limits of three total automatic
entries and 1 USDT cumulative realized loss, crash-safe accounting, prevention
of pre-start signals, manual-close and pause handling, source-time checks,
status/report distinction from the single labelled operator test, and a safe
cutover. These are not present merely because offline research rules exist.
The pilot must not be enabled until those mechanisms and a guarded recovery
have passed targeted and final-candidate Linux checks.

## Important operator-path constraint

The paper exchange itself sends no external orders, but no existing closed
operator path has yet been confirmed for a test BUY while automatic entries
are prohibited. `RuntimeStore.prepare` and `claim` reject BUY when paused.
Running an additional process against the live database is unsafe: the active
Spot worker's recovery loop may observe an order in PREPARED before claim and
mark an accounting contradiction. A safe solution must serialize through the
actual single writer or provide a proven service/lock and crash-recovery plan;
direct SQL order/fill insertion and forged Telegram updates are prohibited.
No test order or trade has been created at this checkpoint.
