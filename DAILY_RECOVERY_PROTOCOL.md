# Mixed daily-trend / minute-monitored hourly RSI2 recovery — offline

Registered before calculation of this policy. This is an independent crypto
adaptation inspired by Connors RSI2, NOT reproduction of the equity strategy or
its returns. Source: https://chartschool.stockcharts.com/table-of-contents/trading-strategies-and-models/trading-strategies/rsi-2
Earlier intraday SMA200, closed-hour RSI, VWAP and other experiments failed;
these same assets and development history have already been inspected. Multiple
research attempts increase selection bias. No untouched-history claim.

Fixed cohort XRPUSDT, ADAUSDT, DOGEUSDT, LINKUSDT, DOTUSDT, including all losing
assets of previous batch. One shared 1000 USDT wallet, one position including
pending actions, LONG and SHORT USD-M simulation, 1x. No asset/parameter grid.

Signals: use only CLOSED one-minute data. Wilder hourly RSI2 is provisionally
updated with latest minute close relative to LAST completed hour close. No final
current-hour OHLC is read. Long: partial RSI moves from <5 to >=5 within SAME UTC
hour; both prices above SMA of last 200 COMPLETED UTC DAILY closes. Short: >95
to <=95 and both prices below daily SMA. First qualifying recovery per asset per
UTC hour only, even if portfolio occupied. Completed-hour Wilder ATR14 / latest
price >=0.006 and remaining distance toward mean of last FOUR completed hourly
closes >=0.006. This mean is equivalent to crossing current provisional SMA5.
Simultaneous candidates ranked known remaining-distance/ATR then lexical symbol;
no fallback after chosen entry rejects. This intrahour recovery is our own rule.

Market entry at minute open +1 minute base, +6 minutes stress. Signal expires at
next UTC hour; delayed fill must still satisfy daily trend, remaining room and
ATR threshold using frozen source hourly/daily references. All fills include
adverse impact 0.0005 base / 0.001 stress per side and fee 0.0005 per side.

Exit: each closed minute crossing current mean of last four completed hours,
with the same delay, OR 12h hard time limit, OR automatic protective stop at two
source completed-hour ATRs, distance clipped [0.005,0.05] from actual entry fill.
Stops use adverse open gap and stop-first minute handling. USD risk estimate
includes stop + roundtrip fees/impact +0.0021 funding stress and stays <=5 USDT.
Notional <=100, 1x, coarse quantity 10 base units if max(fill,mark)<=10 else one
unit. Minimum mark notional proxy50; not verified historical exchange filters.
Actual timestamped archived funding, ambiguous exit-minute credits clipped to0.
No leverage uplift or fills inferred merely from limit touches.

Download only June-Nov2024 additional warmup for daily200; reuse195 pinned
Dec2024-Dec2025 archives. Official public Vision CHECKSUM, max750MiB total,
32MiB/file, four workers, >=2GiB free. No paid API, credentials or VM changes.
Development Jan-Dec2025, warmup from June2024. Publish code/source receipt before
PnL. Checkpoints durable after each cost scenario. Candidate only if base AND
stress net>0, >=30 closed and >=10 each side in BOTH, both stress half-years>0,
stress without best trade>0 and without best asset>0, drawdown<=2%, >=6 active
months. No gate relaxation or loser removal.

Only if development passes: publish result receipt BEFORE downloading2026;
validation Jan-Oct2026, warmup June2025, SAME policy and analogous gates. No
runner-up. No live admission: any eventual candidate remains offline pending
separate owner activation. Existing paper pauses and AI budget untouched.
