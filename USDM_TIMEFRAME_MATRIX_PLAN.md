# Expanded USD-M screening matrix — fixed before screening

Owner replaced the 5m/15m-only request before any futures strategy results.
This plan extends `USDM_INTRADAY_PLAN.md`; the earlier frozen 5m/15m breakout
and range rules remain unchanged. USD-M exchange documentation checked
2026-11-07: [official kline endpoint](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
and [official public-data format](https://github.com/binance/binance-public-data).
Supported kline intervals: 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h,
12h, 1d, 3d, 1w, 1M (calendar month). UTC is used for boundaries; weekly and
monthly bars require their official calendar alignment, not guessed fixed days.

One fixed candidate per applicable cell, LONG, SHORT and one-position joint
simulation separately. Base and stress conditions, taker-only execution,
capital, funding, risk, selection gates and separate leverage analysis are
as in `USDM_INTRADAY_PLAN.md`. A 5m archive cannot establish 1m or 3m prices;
native 1m and its three-bar aggregates are required. If missing, mark those
cells insufficient, never backfill. No retuning after seeing results. Up to
two finalists; no neighbour search is planned or authorized by this plan.

## Fixed windows and applicability

Columns are signal interval, prior breakout-entry closes / breakout-exit
closes / range high-low window, all as bar counts and actual elapsed time;
trend, range and volume windows use the stated range window unless noted.

| Interval | Entry / exit / range | Families |
|---|---|---|
| 1m | 360 / 180 / 720 (6h / 3h / 12h) | A B C D |
| 3m | 120 / 60 / 240 (6h / 3h / 12h) | A B C D |
| 5m | 72 / 36 / 144 (6h / 3h / 12h) | A B C D |
| 15m | 24 / 12 / 48 (6h / 3h / 12h) | A B C D |
| 30m | 12 / 6 / 24 (6h / 3h / 12h) | A B C D |
| 1h | 48 / 24 / 96 (48h / 24h / 96h) | A B C |
| 2h | 24 / 12 / 48 (48h / 24h / 96h) | A B C |
| 4h | 12 / 6 / 24 (48h / 24h / 96h) | A B C |
| 6h | 8 / 4 / 16 (48h / 24h / 96h) | A B C |
| 8h | 6 / 3 / 12 (48h / 24h / 96h) | A B C |
| 12h | 4 / 2 / 8 (48h / 24h / 96h) | A B C |
| 1d | 60 / 30 / 60 (60d / 30d / 60d) | A B C |
| 3d | 60 / 30 / 60 (180d / 90d / 180d) | A B C |
| 1w | 60 / 30 / 60 (420d / 210d / 420d) | A B, context |
| 1M | 60 / 30 / 60 calendar months | A, context; 22-month sample cannot warm up |

E (spread/depth maker scalping) is not numerically screened at any interval:
historical timestamped book, order queue, cancellation, adverse selection and
partial fills are absent. D is deliberately intraday only; C on week/month
and D on >=1h are outside this fixed operational definition, not failures.
Long intervals are not called scalping. Any cell with incomplete prices,
mark/funding, calendar alignment, warmup or too few trades is insufficient.

## Precise family definitions

A: long close crosses above max preceding entry-window closes; short crosses
below min. Exit close below prior exit-window minimum for long, above maximum
for short. Stop 0.75%, max hold twice entry-window duration, except 5m/15m
retain fixed 12h. Conditions and executions use only closed bars.

B: moving average is the prior-only range-window mean close. Long when its
mean rises versus the preceding mean and close crosses from <= prior mean to
> current mean; short symmetrically on falling mean and downward crossing.
Exit on reverse crossing, stop 0.75%, max hold the entry-window duration.
Signals depend on available past, not a retrospective trend label.

C: prior range-window max high/min low width <=2% of midpoint. Long on fresh
entry into lower 20%, never below low; short on fresh entry into upper 20%,
never above high. At fill the execution price must remain in bounds and target
midpoint must be >=0.6% away. Midpoint touch schedules a taker exit; exit
also on close beyond the adverse bound, stop 0.75%, max hold entry-window
duration, except 5m/15m retain fixed 6h. No maker fill from candle touch.

D: with the entry-window previous volume mean, long if close crosses above
previous bar high, candle body >=0.25% of open and volume >=1.5 times that
mean; short reverses direction and crosses previous bar low. Exit on first
opposite close-to-close move, stop 0.75%, maximum hold 2h. No order averaging.

All crossings are evaluated at closes; entry waits for the next eligible
observed bar open. For periods longer than 5m the execution and protection
model uses 5m bars, and for 1m/3m uses 1m bars. Stress adds one such base bar.
An assumed next open is not proof of a fill. Same-bar stop/target or mark
liquidation is resolved adversely; funding ambiguity must remain explicit.
BTC, ETH, SOL have separate cash; no worker or active account is touched.

## Current filters observed before screening

The unauthenticated official `/fapi/v1/exchangeInfo` on 2026-11-07 reported
BTCUSDT `MARKET_LOT_SIZE` min/step 0.001 BTC and `MIN_NOTIONAL` 50 USDT,
ETHUSDT min/step 0.001 ETH and notional 20 USDT, SOLUSDT min/step 0.01 SOL
and notional 5 USDT. Therefore the explicitly authorized 10-USDT cap cannot
pass the **current** BTC or ETH notional minimum, even before quantity
rounding; it must not be raised. These are current metadata, not verified
historical filters or the owner's account-specific limits.

Before any screening results, distinguish (1) the previously fixed $5 and
prior-step hypothetical economic sensitivity, which cannot establish current
exchange executability, from (2) a current-filter eligibility gate. Under (2)
BTC and ETH cannot admit a $10 opening, so a BTC paper candidate requiring
matching present exchange filters is ineligible regardless of hypothetical
returns. SOL alone cannot be selected as a retrospective substitute. This
data-driven correction adds no strategy, parameter search or trading permission.
