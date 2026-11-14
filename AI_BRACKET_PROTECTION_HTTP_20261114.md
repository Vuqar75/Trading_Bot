# Paper protection during slow public HTTP — 2026-11-14

Installed paper runtime `e5e16e31eb63f97595c9daf2fe3898c0631ac48e`; the analyst remains `a0518b9`. The working rules requested by the owner are recorded in AGENTS.md and HANDOFF.md. No real, Testnet, synthetic paper trade or extra paid model request was initiated, and the budget, trading pauses and scheduled analyst timer were not changed.

## Reproduction and correction

In a temporary SQLite scenario with a partially open paper position, a deliberately delayed 300-ms aggregate-trades request held `Poller.cycle`: no second quote was processed during the request, and the stop was detected only on the next cycle. This reproduced local scheduling delay; it does not establish how long an actual exchange request will take.

When a position is open, public quote and trade fetches now run in bounded single-worker executors within the existing process; these workers do not write the ledger. The original financial writer remains single. While a request is pending, the loop can continue pulse and protection checks using only still-fresh saved quotes; when no valid quote is available it preserves the position, blocks unconfirmed entry and waits for fresh data. Time exits remain pending when a valid execution price is unavailable. Completed trade events are checked again against the existing five-second freshness rule before the writer can use them; stale, future, conflicting or failed responses remain fail-closed. A cursor mismatch is rejected. There is no synthetic price and no automatic trade replay.

The fix cannot observe a price while its source is unavailable and does not guarantee a stop fill at its level. Idle or entry-only polling still uses the existing path; the accelerated backlog path remains restricted to a flat account. Long financial batch processing, operating-system stalls and network outages can still delay checks; an in-flight socket timeout is not a hard overall execution deadline. The new active-position path and its real-market latency were not observed on the VM because no position existed; the controlled scenarios are not evidence of a live stop execution.

## Tests and operation

Sixty targeted Linux tests passed using temporary databases, including slow trade HTTP while a position is open, quote unavailability, preservation of TIME exit while a quote is stale, later fresh-price protection, late trade response rejection, restart with a pending request, no duplicate postings, pending entries, partial positions and the prior page-gap tests. An earlier separate reproduction test passed on the previous runtime. The complete application suite was not rerun.

At 23:08–23:09 Baku, 19 ordinary flat-account cycles showed no gap or full page; trade fetch was 251–749 ms and poll intervals 2807–3310 ms. The paper ledger remained balanced with no position, entry remainder, reserve or pending exit. This is an idle health check, not a test of the new active path.

The established external backup helper made a consistent copy before and after switching, downloaded it and verified an isolated restore. Final receipt at 23:09:28.799 Baku: manifest SHA-256 `6403768822d5d7ff1aae863127898302c8bc72427061242e5bd9d6925939908f`, SQLite SHA-256 `609509f91bf42db7cafcba0c96aadfd35a2c02eb09e1483642d9c99d7170d585`. It retained three historical plans, two fills, one funding event and free virtual cash 1000.0090633128153898110 USDT. Encryption is host-bound; cross-host recovery remains unproven. Code rollback is prepared at `/var/lib/tradingbot-ai-budget-backup/bracket-active-e5e16e3`; do not stop protection with open risk or restore an older ledger over later events.

One one-time follow-up is registered for 15 November 2026 00:47 Baku, after the scheduled 00:02 cycle. It will inspect actual saved evidence without extra AI requests or test trades and must not claim active protection was observed if no position occurs. The task is to be removed after execution; no repeated chain is authorized without new data or a testable hypothesis.
