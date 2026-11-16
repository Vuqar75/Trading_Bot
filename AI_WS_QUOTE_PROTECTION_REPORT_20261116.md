# WebSocket quote protection: installed result,16 November2026

Installed paper source **4eedcf05eee37716b79d9a456e149deee81d154c**, confirmed 2026-11-16T12:23:30.841+04:00, `/opt/tradingbot-ai-brackets-4eedcf0-paper`, PID499598. Analyst remains `/opt/tradingbot-ai-brackets-74f77d0-analyst`. No working-market deployment. Last read-only verification 2026-11-16T12:31:31.820+04:00. [Protocol](AI_WS_QUOTE_PROTECTION_PROTOCOL.md), [selected before evidence](research/AI_WS_QUOTE_BEFORE_20261116.json), [installation/observations/restore evidence](research/AI_WS_QUOTE_INSTALL_20261116.json).

## Implemented behavior

The existing StreamFeed now supplies BTCUSDT bid/ask and quantities from public `depth5@100ms`, alongside the existing aggTrade+markPrice@1s market socket. These are two network threads in one existing service; only the existing main writer performs financial actions. Partial top5 snapshots are absolute observations, not diff-depth reconstruction. The small original depth message, normalized BBO, source T/E, receive wall/monotonic time and U/u/pu survive in bounded evidence. Broken pu chains/reconnects/duplicates/revisions/overflow are detected; a fresh absolute quote may recover without reconstructing missing historical executions. No trade replay creates fills.

Every BBO component T/E and mark E must independently pass the unchanged5000ms check at reception and financial use. Mark T is next funding time, never freshness. Composite event time is the oldest source component; fresh mark never rejuvenates old BBO. Network quote I/O is absent from the financial/protection path. Fresh independent component evidence can latch SL even if the other is absent; TIME is a clock condition. First detection, wait/recovery and actual execution remain separate durable evidence. Missing fresh full quote leaves EXIT_PENDING/open quantity; cached disconnected quotes cannot close it. Existing financial handlers, funding and exposure guard remain.

For partial snapshots, a new u can reflect deeper/opposite-side changes. Persistent consumption therefore keys the exit-side price/quantity, not u or mark. Repeated identical visible exit-side states, including after restart, cannot refill the same quantity. This is conservative and can underfill; it is not proof of real replenishment or queue execution. Existing price slippage/fees/risk/size rules are unchanged.

## Why the first WebSocket choice was insufficient

The initial real-time bookTicker variant was not accepted as a successful live repair. Quote-only catch-up triggered excessive financial/snapshot scheduling; that was reproduced and removed (100ms ordinary sleep,20ms only for an actual256-trade backlog). The installed websocket-client1.8.0 also ran redundant pure-Python UTF8 validation. On30000 identical148-byte frames it used10.529593 CPU seconds versus0.764115 with strict native decoding/JSON, same output and malformed UTF8 rejected. TLS defaults and source validation stay enabled. This microbenchmark does not attribute every VM stall to UTF8.

A512-event protective queue overflow also erased independent mark/BBO snapshots. Regression fixed this and bounded history at4096, preserving current absolute components with one unconfirmed overflow cycle. However, real-time bookTicker still accumulated source lag with zero queue overflows: a saved BBO was21929ms behind while mark was fresh. The final partial-depth100ms source limits incoming book traffic at origin rather than hiding stale frames. Source/transport/CPU contributions to every prior delay are not individually proven.

Paper cgroup allowance is now50% CPU rather than20%, within the same VM; MemoryMax128MiB, VM type, billing resources and financial limits unchanged. A focused20% timing fixture failed its existing1s budget (1.307s), while the same22 tests passed at50%. No timing threshold was raised. Native decoder, scheduling and source-rate reductions all matter; these live periods are not a controlled A/B comparison.

## Temporary-ledger tests and actual VM observations

Initial WS integration:169 focused Linux tests passed (brackets,stream,recovery,loop,component/evidence protection). Prior3 resource fixtures passed under20%/128MiB. Final partial-source delta: **28 tests passed in13.057s under50%/128MiB**, including missing/slow REST future, each stale/future component, source-vs-receipt, reconnect/revision/overflow, pu gap and recovery, partial LONG/SHORT SL/TIME, full outage, duplicate, restart and persistent liquidity consumption. No app-wide suite/backtest/model call/test trade. One route fixture initially caught the unreplaced bookTicker URL and was fixed before the final28-test run; an earlier wall-budget burst failure is retained in D: work logs rather than reinterpreted as passing. Unchanged parts reuse earlier evidence.

| Ordinary live period | Unique sampled publications | Confirmed quotes | Quote overflows added | Max main interval | New gap evidence |
|---|---:|---:|---:|---:|---|
| be74bf6,512 queue,300.100s |301|209|143|34308ms|12 saved distinct gap times in sample|
| 9937dd1,4096 queue,300.357s |163|15|0|87493ms|one startup and one later saved gap time|
| **4eedcf0,partial depth,300.086s** |**536**|**536**|**0**|**2501ms**|**only unchanged startup bootstrap; no new gap**|

Final observation 2026-11-16T12:24:08.870+04:00–2026-11-16T12:29:09.155+04:00: actual book receive count+2923, mark+300, trades+8883; quote queue highwater28/4096, trade queue ended0. Both channels one original connection/no reconnect; no fault/overflow. Maximum published component ages book1672ms/mark2161ms, trade batch age2764ms. These are sampled publications, not a complete tick audit. Existing external backup ran during the window. CPU usage57.334237s over300.086s;500/2998 cgroup periods throttled, so scheduler stalls are not declared eliminated. The startup DATA_GAP was at2026-11-16T12:23:27.580+04:00, before the observation; it is not hidden or called a new live fault.

## State, backup and continuation

Sole financial writer/Telegram poller verified:1/1. `/ai orders` existing renderer valid/nonstale, snapshot18771B (limit200000), no new Telegram screen. All four working wallets paused: spot/futures_isolated/futures_cross/margin_cross=1. Older studies/Donchian not enabled; no working services restarted. Existing analyst schedule unchanged:12:02 Baku started and skipped with saved `market_unconfirmed`, no HTTP model call or reserve/retry; old08:02 answer was not relabeled as a new one. Next ordinary opportunity16:02 Baku, subject to unchanged gates. No missed cycle replay.

Ledger unchanged across installation and final check:22events,8plans,4historical fills,1historical funding. Historical plan/state hash `61afbd19137d0745949e616bbc4ecf9d9db4c4582bc571aa05da8f157417ae7e`. Cash1000.309096007865389811USDT; position/remainder/reserve/margin/funding buffer0; reconciliation balanced/no invalid or negative accounts. No artificial position or capital reset. DB21499904B, VM free12245004288B at verification.

Fresh pre-switch external backup `ac71cd1d9f2f94bf91c39a7cd0ebc8bc9d18e882eb430894f626dc1aeac1f904` verified by existing upload/download/isolated-restore procedure, then flat state rechecked before/after stopping only the paper unit. Three runtime hashes match the tested candidate. Code-only previous drop-in/release retained in `/var/lib/tradingbot-ai-budget-backup/ws-quote-4eedcf0/previous-dropins.json`; never roll an old ledger over newer events or stop exposed protection.

Post-switch external restore receipt 2026-11-16T12:27:45.622+04:00: manifest `a85448cb15f886cb523e64cc3e15b263c8c2a4386308bc5d1d77c476c9370a20`, SQLite `436aa0c26ee244839f008d0628974bdd87b07ccdc8d1e65b11008d47e7f0bbd9`,3chunks,22events/4fills/1funding, zero exposure. Host-bound encryption remains a limitation: recovery on a replacement/lost host is not established. Raw phase/test/install files retained in `/var/lib/tradingbot-ai-budget-backup/ws-quote-4eedcf0`; local working evidence on D:/Documents/Codex/stream-work. No C: cache or credentials copied into Git.

Live account was flat throughout the final window. Thus SL/TIME pending→fill under real open risk remains prospective, not demonstrated by these536 healthy flat publications. One one-shot heartbeat **trading-bot-ws-protection-16-02** was actually created/confirmed ACTIVE for **16 November2026,16:47 Asia/Baku (12:47UTC)**, after the next ordinary16:02 opportunity. It must inspect saved outcomes, component timing, first trigger/wait/fill, liquidity,ledger and restore; follow a naturally active plan, with no additional model call/test trade/forced close, and delete itself afterwards. No identical empty-account chain.

No guarantee of execution under total market loss, stale components, missing liquidity, CPU/process failure or unseen intrainterval extremes. The change removes REST quote waiting as a dependency; it does not prove strategy profitability or absolute real-time execution.
