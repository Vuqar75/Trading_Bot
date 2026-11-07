# Fixed Spot intraday comparison — before calculation

Frozen on 2026-11-07 before this candidate's data download and results. One
candidate per rule and timeframe: breakout-5m, breakout-15m, range-5m,
range-15m. BTCUSDT primary, ETHUSDT and SOLUSDT transfer checks, all required.
No tuning, asset selection, adding variants, leverage, shorts or pyramiding.
All 2024–2026 data have been used by the project and are development data.

## Information and rules

UTC Binance Spot 5m OHLCV; 15m bars aggregate exactly three consecutive 5m
bars. Signal uses fully closed bars only. Equal wall-clock windows:

| Rule | Window | 5m bars | 15m bars |
|---|---:|---:|---:|
| breakout entry high of closes | 6h | 72 | 24 |
| breakout exit low of closes | 3h | 36 | 12 |
| range prior high/low | 12h | 144 | 48 |

Breakout: on a fresh crossing, close strictly above maximum previous 6h
closes, buy if flat. Signal exit when close below minimum previous 3h closes;
fixed protective stop 0.75% below executed entry, maximum hold 12h. A crossing
means previous close was not above its own prior 6h maximum. A stop or exit
does not re-arm the same breakout. The shorter stop/time limit relative to the
older 1h 2%/168h rule is an unoptimized fixed risk bound for a 6h horizon, not
a claim about measured volatility.

Range: previous 12h highs/lows define bounds, excluding signal bar. Width must
be <=2% of midpoint, close within bottom 20% and >= prior low; a fresh
transition into that condition triggers a buy only if midpoint is >=0.6%
above the proposed execution price (more than the 0.4% stressed round trip).
At execution reject if price is outside the saved range. Target is saved
midpoint, stop 0.75% below executed entry, exit on close below saved lower
bound, maximum hold 6h. A breakout invalidates new entries. Target crossing
generates a taker exit, not an assumed limit fill. One position maximum.

## Execution and data

At bar close schedule a taker order. Base uses the next 5m open at that same
boundary, an optimistic OHLC approximation rather than proof of an executable
price. Stress skips one complete 5m bar and uses the next open, a 5-minute
additional delay for both signal timeframes. Protective stop is checked each
5m bar after entry, with adverse open for gaps and stop-first for ambiguous
intrabar events. Target crossing schedules exit at the following 5m open;
if target and stop touch within a bar stop wins. No unobserved maker fills.
Protective exits and expiry are evaluated even with no new signal. Unavailable
or discontinuous bars invalidate a segment rather than becoming synthetic data.

Each asset/scenario has 1000 USDT and a fixed maximum 10 USDT notional, no
reinvestment of profits above this amount. Commission 10 bps per side;
base spread/slippage 5 bps per side, stress 10 bps per side; stress also adds
5m latency. Quantity rounded down using prior research assumptions BTC .00001,
ETH .0001, SOL .001; assumed minimum 5 USDT and no depth/partial fills. These
are assumptions, not historical exchange-filter or liquidity evidence.
For attribution, compute two additional execution sensitivities for these same
four rules: raised costs alone (10 bps, zero extra delay), and 5m extra delay
alone (5 bps). These are cost/delay scenarios, not strategy configurations.
Cash and 10-USDT buy-and-hold use the same initial account and costs. Mark
open positions at last close; report hypothetical liquidation with exit costs.

Primary planned range 2025-01-01 through 2026-11-01 UTC, with 2024-12 warmup;
report 2025 H1/H2 and 2026 Jan–May/Jun–Oct independently, starting each in
cash. If verified monthly archives are unavailable, reduce to the longest
predeclared complete calendar segments available and report the missing data;
no filling or selecting favourable months. Data timestamps, checksums, closed
boundaries, continuity, duplicates and UTC aggregation must be checked. USD-M
archive version conflicts are separate and USD-M must not enter Spot results.

## Acceptance fixed before results

Require >=30 completed trades across assets and >=10 BTC trades, >=5 trades in
each segment, positive BTC and aggregate net in the stress scenario, positive
stress net in at least three segments and two of three assets, positive net
after removing the best trade and best asset, account max drawdown <=2%, and
longest losing streak <=12. Useful frequency means >=12 BTC entries per year;
this threshold is operational, not evidence of edge. Require no unresolved
data, timing or accounting errors. Four comparisons introduce selection risk;
none is independent, so passing is at most a candidate for future paper. OHLC
cannot prove actual taker liquidity or fills; a failure of these assumptions
or insufficient evidence yields no recommendation. No optimization afterward.
