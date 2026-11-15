# Direction candidate correction and stream capacity — 2026-11-15

Installed candidate commit `da7343d6f98b1e2206849bf58a7ce6d23a6bc43c`: only `research/ai_limit_plan.py` was overlaid on the existing 5bb85fd paper and a0518b9 analyst runtimes. Installed SHA-256 for that file in both components is `49766897a9cb01fb4bf4e57a4325837ccc9d3489a079967093824fa5f3bf373e`. Release directories `/opt/tradingbot-ai-brackets-da7343d-paper` and `/opt/tradingbot-ai-brackets-da7343d-analyst`; rollback metadata `/var/lib/tradingbot-ai-budget-backup/bracket-targets-da7343d`. Never roll back the ledger or interrupt protection under open exposure.

## Causal snapshots

Ten saved BTCUSDT model inputs from 13 Nov 07:42 through 15 Nov 00:02 UTC were reconstructed from their original information, not later candles. For each, the candidate count in original → corrected implementation (LONG/SHORT):

| UTC | Original | Corrected |
|---|---:|---:|
| 13 07:42 | 3/4 | 4/4 |
| 13 12:02 | 1/4 | 4/4 |
| 13 16:02 | 0/0 | 4/4 |
| 13 20:02 | 0/1 | 4/4 |
| 14 00:02 | 4/0 | 4/4 |
| 14 08:02 | 2/0 | 4/4 |
| 14 12:02 | 0/0 | 4/2 |
| 14 16:02 | 0/0 | 0/0 |
| 14 20:02 | 2/2 | 4/4 |
| 15 00:02 | 4/0 | 4/4 |

At 04:02 Baku the saved 15m ATR was 653.67142857, bid/ask 64094.2/64094.3. Of 36 originally tested SHORT combinations, 30 failed net reward/risk and six failed volatility-relative stop distance. Original three closest target cutoff removed viable confirmed supports before full validation: including all already available targets produces four SHORT candidates at entry 65337.1, stops 65922.7 or 65999.6, targets 63336.4 or 63358.6, net reward/risk 1.9078–2.1058. These were not actual plans and cannot be executed retroactively. The candidate set changes the model input and does not prove the model would accept those candidates, particularly where nearer levels intervene. Original saved response was WAIT, citing long entries against downtrend. Other candidate caps remain. No LONG-only directive, wrong symbol or sign asymmetry was established. The existing stop, costs, size, filters and risk tests remain in force.

The changed candidate generator and synthetic full-page replay passed 72 targeted Linux tests on an isolated candidate tree. Before switching, the existing backup helper uploaded and downloaded an encrypted external backup and validated isolated restoration; manifest SHA-256 `b2e8a0386c417e85a36ac772970004ed51240fd6310e53838b0e04b5f2578984`, SQLite SHA-256 `5c36bb20fc4fae31f600ce5e1898743b35a1849285fd1d81cf2a9f7a32c9812d`. Switch was made flat, without changing financial data or analyst timer. At 03:47 UTC the live paper service and analyst timer were active, ledger balanced, free cash 1000.009063312815389811, position and reservation zero. The established external backup service completed successfully at 03:47:24 UTC after installation; host-bound encryption remains a recovery limitation.

## Stream remains unresolved

At the beginning of this work, 22 of 27 historical DATA_GAPs reported full 1000-event pages. One preserved page 3524821877–3524822876 spanned 31 ms in exchange event timestamps; the subsequent 1000-row page was already stale when received. Exchange event-time density is not a proven network arrival rate or exact amount lost. The existing active-risk path cannot declare the stream current when full, while the idle path is bounded to two accelerated cycles. A temporary database replay of these timestamps confirmed fail-closed handling and no retrospective execution; it did not demonstrate that the live polling scheme keeps up with the burst. A separately measured synthetic active-position page took about 0.672 seconds in a single run on a temporary database; it does not establish throughput under VM CPU limits or sustained load. A read-only WebSocket probe matched 100 consecutive public stream trades to the REST record, but this does not verify reconnection, bounded buffering, missing-ID recovery, funding, financial writer, or protection under an active order. No transport replacement has been implemented or installed. The last observed idle DATA_GAP at 03:45:26 UTC had 214 rows, five stale, no ID gap; current market errors were clear by 03:47. Do not claim throughput or live protection fixed; restoring safe stream capacity remains unfinished.

The obsolete one-time diagnostic automation was removed to honor the owner's reprioritization. No new paid request, artificial trade, capital or risk change was made. A scheduled analyst opportunity remains at 04:02 UTC; its outcome is not part of this report.
