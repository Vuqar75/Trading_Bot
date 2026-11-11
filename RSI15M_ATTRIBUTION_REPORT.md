# RSI15m execution diagnosis and follow-up screening

## Four-cell diagnosis

Frozen before the two additional calculations in `2ceb914`. Reused the original
two completed cells; no rerun, changed signals, parameter search or downloads.
All cells are separate 1000-USDT model accounts for BTC USD-M 2025, 1x and
notional <=100 USDT. Fee remains5 bp per side. Impact includes spread.

| Entry delay | Impact per side | Closed | Gross USDT | Fees | Impact | Funding | Net USDT |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1 minute | 5 bp | 42 | 15.708673 | 3.730702 | 3.730706 | +0.173287 | **+8.420553** |
| 1 minute | 10 bp | 41 | 15.845219 | 3.654388 | 7.308792 | +0.156911 | **+5.038950** |
| 6 minutes | 5 bp | 40 | 8.734575 | 3.566026 | 3.566028 | +0.158926 | **+1.761448** |
| 6 minutes | 10 bp | 40 | 6.996587 | 3.567438 | 7.134883 | +0.186745 | **−3.518989** |

Net deterioration across the original diagonal is11.939542 USDT. Averaging
marginal changes over both intervention orders attributes −7.608522 to delay
and −4.331020 to impact. These are symmetric model-path attributions, including
changes in stop/target levels, accepted trades and subsequent exposure. They
are not independently measured market effects or pure fee deductions.

The result does not justify a maker implementation. There is no maker-fill
model here. Nor does it justify installing the fastest profitable historical
cell: that would change admission after seeing the stress result.

The unchanged execution diagnosis also shows an independent mismatch with the
owner's intraday objective: only80–83% of closed trades finish within24h in
all four cells, versus the registered90%. Q3 has no closed trades in every
cell. The slow/high-impact case has only one positive quarter. Execution
improvement alone has not solved the original admission failures. Candidate
remains rejected. No SHORT-only or new exit rule was selected retrospectively.

All81 added counterfactual trades passed a separate saved-ledger audit of net,
reference gross, fixed impact formula, both-side fees, funding sums, entry
delay, nonoverlap and size/risk limits. Cash reconciles and no open/pending or
unresolved position remains. Full journals and summaries are retained. No
full application test suite was warranted by these isolated research scripts.

## Additional public-source screening

Foreign source was inspected statically, never executed or imported. Source
commits and per-file SHA256 inventories are in
`research/external_strategy_review/followup_receipts.json`.

### VWAP lab

Source: https://github.com/ojeology/mean-reversion-vwap-lab
Commit: `5ae2853d729b6761d4be3c2ccb7602beb32665cf`.

`backtest_v14_honest.py` uses next-bar open entries (not same-bar lookahead).
TP/SL selection is restricted to preceding training windows, which is an
improvement over its explicitly corrected older full-period optimization.
However, same-minute TP/SL ambiguity chooses TP (`ti <= si`), stops fill at
exact levels even on gaps, funding/slippage are absent and the commission is
fixed$0.08 on modeled$100 notional. It combines MEXC Spot public candles with
20x synthetic LONG/SHORT PnL, without a demonstrated futures execution model.
A minute touching both1% stop and2% target at100 notional would report+1.92
rather than adverse-first−1.08 USDT: this example demonstrates the assumption,
not the number of affected historical trades (that number is unknown).

The saved honest artifact has15 test days,184 trades,+87.82 USDT and PF1.7367;
it is not the same artifact as the headline older8/8 scorecard. Pair/hour
selection follows earlier experiments on overlapping research history, so TP/SL
walk-forward alone does not make the entire selection process independent.
No pinned raw candle dataset or complete trade-level forward record was found
among tracked files. Do not treat headline returns as verified Binance BTC/ETH
futures evidence or launch a collector from them. This does not prove that a
properly specified VWAP strategy is unprofitable.

### Cross-sectional momentum

Source: https://github.com/Gautam-Kanna/crypto-momentum-reversal
Commit: `84180420d9c2ee432a8d7da470a7d393a48b148d`.

The author describes4h bars but uses3/7/14/30-day momentum and8-day weight
smoothing across10 assets; this is not a5m/15m single-position strategy. The
README reports OOS Sharpe0.41 and alpha t-stat0.82, acknowledges survivorship,
free shorting/funding omission and close-price fills. Its short-horizon reversal
versions do not survive its20-bp cost assumption. Do not transfer its positive
slow-portfolio result to an intraday BTC LONG/SHORT bot. No new backtest run.

A third search hit, PravarAgarwal/intraday-trading-research, is an NSE RELIANCE
cash-equity ORB example, not verified crypto futures evidence; its quoted PF
must not be transferred between markets.

## Operational scope

VM, bot, pauses, secrets, budget and paid schedules unchanged. Only cloud
research checkout and local archive replay were used. No paid API requests,
new services, local-agent tasks or strategy activation. Completed rejected
experiments are not queued again. Full goal of a demonstrated profitable
short-term bot remains unresolved; no claim of success is made.
