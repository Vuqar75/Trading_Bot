# Frozen independent RSI15m check — 11 November 2026

Registered before loading market data or calculating this configuration.
Source: Dnayaka/btc-rsi-momentum commit
`3216d14f111d705bd48b3b1746bd7cbaa2832653`, v20 defaults, Pine SHA
`f1a692ebc926a52ad30a29e5ce5f3975b85fa6b9ab65ff6de856a241f0e13930`.

One candidate, BTCUSDT USD-M, 2025 full year UTC; December 2024 warmup. The
year is deliberately the difficult year described in the source, selected
before calculation, not an untouched OOS period. No optimization, alternate
version, asset or timeframe. Published return is not assumed correct. This is
an independent **protected execution adaptation**, not replication of the
unavailable author Python harness or delayed initial Pine bracket.

Signal: exact v20 base and pullback state machines on closed 15m bars. RSI14
crosses above 82 for LONG, below 20 for SHORT; candle direction, EMA200 buffer
0.2%, ATR14/close in [0.2%,1%], body/range>=0.3. Base LONG additionally
close/EMA200-1>=1.5%; SHORT has no symmetric 1.5% filter. Pullback is armed
on raw RSI breakout on the correct EMA side, watches 28 bars, requires 1%
retrace from updated peak/trough, freezes that extreme, then closes beyond it
with candle/ATR/body confirmation. Arming resets on raw breakout, expires
after the watch bar or loss of EMA side. Both state machines update regardless
of open position. Same-direction cooldown: six 15m bars after last close.
Opposite signals can reverse; same-direction signals do not add exposure.

Pine RSI/RMA SMA seed; EMA seed first close. TP: 2%, or 2.15% if signal-bar
ATR%>0.40. Stop: 1.9%, multiplied by 1.3 in v20's literal macro window. Use
signal-bar open time America/New_York: FOMC 2025 dates exactly Jan29/Mar19/
May7/Jun18/Jul30/Sep17/Oct29/Dec10, ET hours12..17 inclusive; NFP heuristic
first Friday, hours6..11 inclusive. This is source code, not a certification
that this heuristic matches actual macro releases. Trailing activation2.5%
exceeds both default targets; with our immediate protective bracket TP occurs
first, so trailing is dormant. No artificial maximum holding time.

Execution: existing research Account financial methods, separate 1000-USDT
base/stress cash accounts, quantity fixed after entry, notional<=100 USDT,
1x, current-filter proxy BTC minQty/step0.001 and minNotional50, modeled risk
including full costs and 21bps funding reserve<=5 USDT. Fresh valid trade/mark
minute reference required. Protection active immediately after fill; this
differs from default Pine recalculation. Entry after signal at T+1min base,
T+6min stress. Fee5bps/side; combined spread/impact5bps base,10bps stress.
All exit orders costed as taker, including TP (conservative relative to resting
limits). Stop before TP if a minute touches both; stop gaps use adverse open;
TP gets its level even on favorable gap, with exit impact. Actual saved funding
events with conservative stop/funding order, no zero fill. Existing Account
mark-based risk and drawdown assumptions retained. No real fills asserted.

No mass application import: reuse the existing research Account class only,
without importing the unrelated unavailable XGBoost training dependency.
No changes to shared Account, application, VM, budget, schedules or secrets.

Before calculation pin all 13x3 source archive SHA256 checksums. If unavailable
or contradictory inputs occur, do not invent repairs; report limitation.

Qualification for a future research candidate: >=30 closed trades, >=10 each
direction; both scenarios net including residual positive; stress without best
trade positive; at least3/4 calendar quarters positive in stress; MTM DD<=2%
in both; >=90% closed trades within24h. This does not prove future profitability.
Otherwise reject this fixed adaptation or report insufficient data. Never tune
parameters or switch versions after results. Save complete signals/trades,
cost/funding decomposition, residual, rejections and source evidence. No install
or activation, even on a positive result. Targeted temporal/accounting checks;
no full app suite. Public archive reads only, no paid API calls.
