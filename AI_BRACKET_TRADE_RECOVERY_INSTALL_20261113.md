# Trade-stream recovery: VM installation, 2026-11-13

Installed exact candidate `4fb90fa` in the separate release `/opt/tradingbot-ai-brackets-4fb90fa`. Both the existing paper and analyst services use it; the Telegram release remains `/opt/tradingbot-ai-brackets-ui-c96ad52`. The three runtime files match the SHA-256 values in `AI_BRACKET_TRADE_RECOVERY_PATCH.json`. The previous cancellation diagnostic and idle-tick fixes were preserved; model, trade/risk rules, budget and schedules were not modified.

Before switching, live status and a consistent external backup established no open position, pending entry, reserve, margin or funding buffer. The analyst timer was briefly stopped and restored to its original enabled/active state; no analyst run was started or interrupted. The paper service was stopped only after another fresh public-state check, followed by a direct terminal-state check on the database. A timed rollback and the original unit overrides are retained under `/var/lib/tradingbot-ai-budget-backup/bracket-recovery-4fb90fa`. Never stop protection or replace newer database events when rolling back an active position. Telegram and the working trading services were not restarted.

All 43 targeted tests passed on Linux in a separate temporary checkout with temporary HOME and no model credentials. The candidate's real startup and ordinary cycles were then observed without injecting a trade or a model request:

| Baku time | BTC public aggregate-trade observation | Admission state |
| --- | --- | --- |
| 16:53:46.644 | Post-restart batch had 11 stale rows; idle bootstrap fetched 1000 latest records and discarded it for execution. | `public_trade_stream_unconfirmed` |
| 16:54:05.719 | 126 fresh rows, first ID 3519959536 after 3519959535, no gap, newest age 267 ms. | Confirmed |
| 16:54:09.926 | 22 rows with two stale, no ID discontinuity; idle resync. | `public_trade_stream_unconfirmed` |
| 16:54:13.594 | 56 fresh rows, first ID 3519959685 after 3519959684, no gap, newest age 219 ms. | Confirmed |

At 16:55:26.982 a later fresh contiguous batch was confirmed and `market_errors.BTCUSDT` was null. A later persisted gap at 16:55:03.743 had 11 stale rows, zero future rows and no recorded ID discontinuity. These observations show intermittent source freshness issues, not the historical cause of the 16:02 cancellation and not proof of a Binance outage. Only bounded diagnostics and batch hashes are retained, so raw batches cannot be reconstructed from hashes. A future gap must continue to block admission until a fresh contiguous batch confirms recovery.

The actual BTC `/ai orders` renderer was checked as `tradingbot` with sockets forbidden: the earlier 69466.2 entry still shows the saved `TRADE_GAP` cancellation and 16:02:40 Baku time. This establishes message formation, not owner receipt. Both historical plan IDs, two fills, one funding event, free balance 1000.009063312815389811 USDT and balanced ledger were preserved; no open position or entry remained at the check. The four working-market pauses stayed set; one existing Telegram poller was running. The analyst timer was restored, with its next opportunity 16:02 UTC / 20:02 Baku subject to admission gates. Budget remained ten attempts and $0.023814 reserved. No extra AI request or paper trade was made.

The existing encrypted external backup succeeded both before and after the switch, downloading and restoring the separate ledger in isolation without an active second writer. The post-install manifest SHA-256 is `d67c0343f09aae7c7f6bf1e5a3df49043c03aadc96c7f044bf383e8213a3965f`, object `paper-backups/daily/ai-bracket-study/1794574498341-d67c0343f09aae7c7f6bf1e5a3df49043c03aadc96c7f044bf383e8213a3965f.cred`; restored counts and balances match. Encryption remains host-bound; restoration to another VM is not established.
