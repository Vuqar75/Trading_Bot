# One fixed BTC/ETH relative-value screen

Registered before calculation on 2026-11-12. This is an OFFLINE hypothesis
screen, not an executable strategy or authorization to change VM allocation.
No grid, new model, paid request, collector or installation.

Mechanism: a temporary extreme in ETH/BTC might reverse while a simultaneous
opposite BTC leg reduces common market exposure. General motivation is pairs
trading, Gatev, Goetzmann and Rouwenhorst, NBER w7032:
https://www.nber.org/papers/w7032 . The primary abstract was read today. Its
US equity, normalized-distance, six-month portfolios are NOT evidence that
this new crypto intraday adaptation is profitable. Fixed equal-dollar legs
are not a fitted beta hedge and log-price ratios need not be stationary.

Use only already pinned BTC/ETH USD-M minute trade/mark/funding archives,
2024-12 warmup, 2025-01 through 2026-10 evaluation. This history has been used
by the project: no untouched holdout or independent validation claim.

At each closed UTC 15m candle let s=log(ETH close/BTC close). Compare with
the mean and sample standard deviation of the PREVIOUS 96 closes (one day),
excluding the signal close. Enter only at a crossing from |z|<2 to z>=2
(SHORT ETH / LONG BTC), or from |z|<2 to z<=-2 (LONG ETH / SHORT BTC).
Require |s-mean|/2 >= 0.0035: approximately 30 bps stress round-trip cost
on combined gross notional plus 5 bps margin. This is a hypothesized move,
not a forecast of attainable profit. No reuse of yesterday's crossing.

Both legs use equal entry-reference dollar notionals. Entry reference is the
minute open signal+1 minute (base), +6 minutes (stress). Exit BOTH at the
minute open exactly 240 minutes after entry. One concurrent pair per scenario;
ignore crossings while occupied, allow a new signal at the previous exit.
No stop, target, refitting or mean-touch exit in this first screen. Report
the most adverse joint minute mark-price bound during each holding period.
Consequently this screen cannot establish a <=5 USDT stop guarantee or be
activated even if its arithmetic result is positive.

Taker fee 0.0005 per side, impact 0.0005 base / 0.001 stress, separately
on EACH leg and entry/exit. Funding uses each symbol's pinned rate and mark,
only events strictly after entry and strictly before exit (positions are
flat at the exit boundary). Opposite leg funding must not be omitted.
Normalize quantities to 1 reference USDT PER LEG; report bps divided by
2 initial gross USDT. This is a return/opportunity screen, not exchange fills,
finite lot sizes or a funded account simulation. No notional/risk limit on
the working bot is changed. Historical minima, legging/queue/partial fills,
funding boundary micro-timing and actual spreads remain unverified.

Predeclared continuation screen: >=60 nonoverlapping pairs per scenario,
>=10 each direction; base AND stress mean net >0; stress mean net >0 in
at least 3 of 4 exit-date segments (2025H1/H2,2026H1/H2); stress aggregate
net remains >0 without its best pair. Otherwise reject THIS configuration,
with no tuning/alternate pair after results. A pass permits only a separate
execution/risk feasibility assessment, never immediate live/paper admission.
Preserve full case journal, source hashes, excluded end windows, financial
decomposition, duration and period/direction counts. Verify causality by
prefix truncation and audit both-leg PnL independently. Reuse source loader;
do not refit any completed RF/XGBoost/EMA experiment or run full app suite.
