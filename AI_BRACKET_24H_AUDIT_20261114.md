# AI bracket decisions and trade feed: 24-hour audit, 2026-11-14

Read-only VM snapshot at 22:22 Baku on 14 November, covering 13 November 22:22 through 14 November 22:22. Times below are Baku (UTC+4). The current installed paper worker is `007b53faaa4f64d21a8674d2233cea1030979bba`, analyst `a0518b9`. No paid request, test trade, service change, replay or new test suite was initiated for this audit.

| Boundary | Actual result | Saved candidates sent to model | Paper action |
|---|---|---:|---|
| 00:02 | SHORT, validated | 1 SHORT | Armed 00:02:30.604, canceled 00:04:15.442 for TRADE_GAP, no fill; reserve 69.64681212 released |
| 04:02 | WAIT | 4 LONG | Model rejected LONGs against higher-timeframe weakness and nearer resistance; no order |
| 08:02 | skipped before model | unknown | `not_requested_market_or_funding_unconfirmed`; old combined gate did not persist which condition; no request or reserve |
| 12:02 | WAIT | 2 LONG | Model found LONGs contrary to higher-timeframe context and targets beyond resistance; insufficient case for SHORT |
| 16:02 | WAIT | 0 | No mechanically eligible candidates; model also described mixed context |
| 20:02 | WAIT | 0 | No mechanically eligible candidates; model cited lack of a plan despite weak trend |

Five saved model responses and usage were matched to the five runs; none recorded a network retry. Those five responses have total estimated API cost $0.0031884 and reservation $0.012841, which are different accounting views, not additive expenses. No executed trade or paper commission/funding arose in this 24-hour interval. There is one validated and armed plan, lost without fill at the protection gate due to DATA_GAP, not lost in message delivery. No further valid plan was approved in the window. The earlier closed position and its historical net +0.009063312815389811 USDT are outside this period.

## Candidate arithmetic

Using the saved snapshots and the installed algorithm's enumerated entry/stop/target limits, validation at each snapshot's original `as_of_ms` was repeated locally without network. For 00/04/12/16/20 respectively: combinations 57/57/45/63/24, passed and sent 1/4/2/0/0. Total 246 combinations; 239 rejected at the first validation failure: 188 `net_reward_risk` (full modeled costs/risk-reward below 1.5), 47 `stop_vs_volatility`, 4 `buffered_level_geometry`. This is a count of attempted combinations, not independent signals or trades. Six offered LONG combinations were declined in the two nonempty WAIT decisions; the model gave a shared reason per response and did not independently label each candidate. Two WAITs contained no candidates. The earlier market/funding gate precluded a saved snapshot for 08:02, so its possible candidates and individual cause cannot be reconstructed. No assertion is made about missed unrecorded signals or confidence of the strategy.

## Feed after asynchronous filters

Seven `DATA_GAP` records after installation through the check, all with `active_armed_ms=null`; first was the known restart backlog. Four of seven returned the page limit of 1000 trades, with 1000/1000/155/758 stale rows respectively, no saved ID discontinuity. A full page and stale cursor demonstrate backlog saturation, but the saved per-gap history is not sufficient to determine whether burst rate, HTTP latency or another condition initiated each episode. The current bounded diagnostic stores only the most recent gap's full timings, so past per-HTTP timing for the other gaps cannot be reconstructed.

At the latest gap 21:42:43.421, 141 contiguous trades included 3 stale, oldest 5005 ms; prior cycle poll work 1954 ms and `aggTrades` HTTP 1407 ms, sleep 2000 ms, quote calls 255+255 ms, current trade HTTP 259 ms, SQLite/financial and publish phases small. This identifies the slow request's contribution but does not establish its network/exchange cause or prove it alone would always cause a gap. Earlier 21:20:08 had poll interval 5601 ms, no detailed contemporaneous phase record remains. No arbitrary timeout increase or unsafe backfill has been made. At 22:22 the feed was confirmed and contiguous, 139 rows, oldest 3140 ms, 0 stale; no open position, order remainder, margin, reserve or pending exit. Ledger reconciliation succeeded. A flat snapshot is not verification of live protection with risk.

A full 1000-row page followed by the fixed two-second poll cadence is a confirmed throughput limitation, not enough evidence to claim a particular bug fix or safe active-order pagination from the retained data. No behavior change was installed on this audit. A specific bounded follow-up experiment, not authorized for execution by this audit, is to measure successive page arrival rates and cursor lag in the existing public process over three 30-minute windows with no extra model calls, retaining capped aggregate telemetry rather than raw trade bodies. Hypothesis: bursts persistently exceed page-drain throughput. Predefine success as at least 10 full-page sequences with contemporaneous cursor-lag evidence and a safe replay on temporary SQLite demonstrating bounded catch-up without stale fills or loss of protection. It requires no paid resources; it cannot establish economic edge and should not alter thresholds or orders.

Latest external backup receipt captured 21:27:16.536 Baku, manifest SHA-256 `c69cdfb47262c029ee419052ba51b40b130335c9588792e61d78bc2968f02511`; existing backup process reports download and isolated restore validation. Encryption remains host-bound. Next analyst opportunity is 15 November 00:02 Baku, subject to admission gates.
