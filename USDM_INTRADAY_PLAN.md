# USD-M perpetual 5m/15m comparison — preregistration

Fixed before candidate results, 2026-11-07. Four rules, no parameter search:
breakout 5m/15m and range 5m/15m, each long-only, short-only and combined;
one position per BTCUSDT/ETHUSDT/SOLUSDT account. BTC is primary. Market:
Binance USD-M perpetual, not Spot, COIN-M, index or mark as trade price.
Same 2025H1, 2025H2, 2026 Jan–May, Jun–Oct segments and 2024-12 warmup as
the preceding Spot research; all development data, not independent OOS.

Use previous `SPOT_5M_15M_PLAN.md` long rules verbatim, including 6h/3h
breakout, 12h range, 0.75% stop, 12h/6h maximum hold, one crossing and
5m-bar target/stop model. Short breakout: close strictly below previous 6h
minimum of closes, fresh crossing only; exit close above previous 3h maximum,
stop 0.75% above entry. Range SHORT: prior range width <=2%, close in top 20%
and <= prior high; fresh transition only, enter only if executable price is
within range and at least 0.6% above midpoint; target midpoint, stop 0.75%
above entry, exit when close exceeds saved upper bound, 6h maximum. A range
break rejects entry. Simultaneous opposite signals cancel; no new position
while occupied. Combined uses identical signals and single-position policy,
not the sum of separately traded accounts.

Signal bar close then first next 5m open (model assumption, not proven
obtainable quote); stress waits an extra full 5m bar. Stop-first, adverse
gap, target touch schedules next-open taker exit, no maker-touch fill.
Base taker 5 bps/side plus 5 bps spread/slippage/side; stress 5+10 bps and
5m delay. Current official VIP0 maker 2 bps/taker 5 bps, without BNB or
special pricing; account rate and historical rates are not confirmed.
Fee-only attribution reruns same futures LONG inputs with 10 bps taker,
unchanged 5 bps impact and funding; compare with 5 bps taker. Futures price
attribution compares that 10-bps futures LONG with prior same-rule Spot LONG
but must also isolate funding and document any trade-set differences. SHORT
attribution compares combined and long-only on identical futures data and
conditions; overlapping exposure makes differences non-additive.

Use public trade and mark 5m OHLC plus actual funding event rate/time. Apply
funding only when entry time <= event < exit time; positive funding costs
long and benefits short. If an event falls inside a 5m bar, use that bar's
mark open, explicitly approximate; conservatively flag ordering ambiguity with
an intrabar stop. Gaps, changed source events and missing mark/funding are not
zero. Historical step/minimum filters are not known: use prior study quantity
steps and 5 USDT assumed minimum, separately mark present metadata as current.
For a first-order isolated liquidation check, fixed 1x initial margin equals
notional, maintenance 0.5% of marked notional and hypothetical liquidation fee
1% of liquidation notional, explicitly conservative model assumptions, not
verified historical Binance brackets. Mark OHLC drives the risk condition;
trade-price OHLC drives stop and fill. If mark and stop both trigger in one
bar, count liquidation first; trade slippage and bankruptcy gaps remain
unresolved. No partial fills/partial liquidation and no guarantee of exit.

Initial 1000 USDT per asset/segment/scenario, fixed <=10 USDT notional, no
reinvestment, 1x and no transfer. Reset to cash each segment; unfinished
positions mark with hypothetical exit costs, not completed trades. Cash
baseline 0. Accounting includes commission, impact and signed funding.
Account drawdown is separate from return on initial margin. Attribution must
not label known historical source-version conflicts as settled.

Acceptance requires >=30 completed combined trades across assets, >=10 BTC
trades and >=5 combined trades per segment; >=12 BTC entries/year, stress net
positive overall and BTC, positive net in at least three segments and two
assets, positive without the best trade or asset, account max DD <=2%, max
losing streak <=12, complete consistent source/mark/funding and no unresolved
arithmetic or liquidation-model errors. Four variants incur selection risk;
even a pass is only an offline paper candidate. OHLC cannot establish actual
book execution. Unknown data/rules or failed gates prevent installation.

If no variant passes at 1x, do not backtest scaled leverage: explain fixed
notional invariance and equal-margin/risk arithmetic, test liquidation model
with fixtures, and reject. Only a passing candidate permits full 1x/2x/3x
equal-notional, equal-margin and equal-risk analysis, then optional separate
isolated experiment. No live trading, AI spending or working-market changes.
