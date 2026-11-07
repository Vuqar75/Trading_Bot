# BTCUSDT paper Spot: pilot preparation — 2026-11-07

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
