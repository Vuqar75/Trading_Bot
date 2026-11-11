# Fixed small-account trend-retest hypothesis

Registered before calculating results. This is a new protected small-account
adaptation of PravarP11/multi-timeframe-trend-retest-strategy at
f750f2ba12a83ace5de0f2e6d056bfcdaa6fe171, not author-return replication.

BTCUSDT USD-M calendar2025 UTC, December2024 warmup; known development data.
No asset or parameter sweep. Closed15m EMA9/15 crossover arms direction only
with price beyond both last completed4h EMA100/200 and a matching EMA100 slope
vs16 prior15m rows. Crossover volume must exceed2x50-bar volume mean including
current bar. Opposite cross disarms the old direction. At most20 bars after
cross, enter on low<=EMA9 and close>EMA15 (LONG), mirrored SHORT, ADX14>=20,
and absolute distance from EMA9<=1%. Crossover-bar retest allowed as in source.
Consume one entry signal per armed cross. No pyramiding or hedging.

One fixed protected position; stop1% and full target1.5% from fill. Full target
is a declared adaptation because half of minimum0.001BTC cannot be closed on
its0.001 step at our<=100USDT cap. Source's partial+trailing is NOT reproduced.
Exit at first15m close at least100bars after entry, then normal1/6min delay.
No ATR exit is used; source ATR is a Wilder sum rather than mean (14x), noted
but irrelevant to this adaptation. No re-entry following consumed signal.

Separate1000USDT cash accounts,1x,notional<=100,model risk<=5,minqty/step.001,
minnotional50 proxies. Funding, mark, adverse bracket handling and accounting
reuse the pinned RSI execution class. Base delay1min/impact5bp per side;
stress6min/impact10bp; both fees5bp per side. Stop has priority on same-minute
TP/SL ambiguity. No real fills or historical filters asserted.

Qualification unchanged:>=30 closed,>=10LONG and10SHORT,positive net both,
stresswithoutbest positive,>=3positive quarters stress,MTMDD<=2%,>=90%closed
within24h. No fitting to this outcome. If these fail, reject without install.
If they pass, require a separate fixed-rule validation before paper admission.
No VM, pauses, AI, budget, secret or working financial state changes. No paid
calls, new collector or full application suite; focused temporal/ledger audit.
