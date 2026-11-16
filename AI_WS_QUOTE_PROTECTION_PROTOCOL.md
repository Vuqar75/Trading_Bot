# USD-M WebSocket quote protection protocol — 2026-11-16

Scope: replace REST quote dependency of the existing paper Poller only. No new service, collector, financial writer, model call, strategy/risk change or order transport. Existing AI analyst continues its saved-budget scheduled REST capture; its availability does not drive local protection.

Official sources checked16 November2026:
- [USD-M Public streams / individual book ticker](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/public#individual-symbol-book-ticker-streams): public route, event E, transaction T, update u, BBO price/quantity b/B/a/A.
- [USD-M Market streams / mark price](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market#mark-price-stream): event E is mark source time; T is NEXT funding time, not quote timestamp. The1s stream is used.

One existing StreamFeed, two routed sockets per configured symbol, initially BTCUSDT:
`wss://fstream.binance.com/public/ws/btcusdt@bookTicker`
`wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade/btcusdt@markPrice@1s`
Both network threads only update bounded memory. The existing main process owns the ledger. The market socket demultiplexes verified combined envelopes; wrong symbols/type including COIN-M st=2 are rejected. No keys or private/Testnet endpoints.

The existing32768 trade queue/batch256 and continuity/reconnect latch remain. New quote memory: one latest absolute BBO, one latest mark, at most512 protective component events; overflow is recorded and execution unconfirmed. This is top-of-book absolute data, not diff-depth reconstruction or maker-fill proof. Book u need not increment by one. Repeats do not refresh receipt or source age; revised same identity is rejected until a newer identity. Older identities are ignored without retroactive execution. Connection loss clears its component; recovery requires a new absolute component on that connection. Trade-history faults remain independent of fresh quote-based protection and require the existing safe trade recovery; no missing trade executions are replayed.

Every component independently checks source and event timestamps against the unchanged5000ms bound, future time, and receipt ordering. BBO freshness uses T AND E; mark freshness uses E. Composite execution time=min(component source times), received=max(component receipt times); local receipt never replaces source time. Both channels/components must be confirmed for any protective fill. Missing/stale/disconnected/revised/overflowed quote evidence blocks execution even if a previously cached quote is younger than5s. Public status includes quote_feeds separately from trade_feeds.

Fresh independent component events can latch SL (mark or exit-side BBO), only if their source is at/after opening and still fresh at actual observation. This does not fill: incomplete full quotes leave EXIT_PENDING. TIME remains a clock condition and is retained even without any market. The writer saves first detection (wall + monotonic), source component evidence, wait/recovery, and actual exit quote/fill separately, atomically with existing state. Protection is checked before bounded trade-batch processing. No future/result price, synthetic price, late execution or loosened limit.

BBO liquidity identity depends on symbol/u/T/prices/quantities, independently of mark updates and reconnections. A new composite quote with the same BBO cannot reuse already consumed visible quantity. Partial exits retain pending state across restart; new admissible BBO liquidity may close the remainder. Existing fees/slippage/funding/reconciliation and idempotent ledger handlers are reused. No guarantee of fills during complete market loss, partial liquidity, source delays or process failure.

Acceptance: temporary-ledger tests for missing/stale/future component, source-vs-receipt semantics, blocked REST future, channel reconnect/revision/overflow, LONG/SHORT stop detection, TIME with total outage, partial exit/mark-only updates/restart/no duplicate postings. Linux target suite only; no unchanged backtests or full app suite. Safe flat switch after fresh external backup/download/restore, code-only rollback with new financial events retained; normal live stream observed separately from fixture risk coverage.
