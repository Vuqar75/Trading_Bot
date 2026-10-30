# Stage 1 results — 2026-10-30

Isolated research only. No deployment, no live database or Telegram configuration access.

## Dataset and validation

- 32,488 continuous one-minute Spot Testnet candles; 2026-10-07 10:32 to 2026-10-29 23:59 UTC.
- Requested 30 days; first 10,712 minutes were unavailable. No synthetic gap filling.
- 8 zero-volume candles; 34 close-to-close moves above 1%; maximum 24.692167% at 2026-10-28 06:35 UTC.
- Training decisions: 2026-10-07 11:03 through 2026-10-19 23:58 UTC; force-close at 23:59.
- Holdout decisions: 2026-10-20 00:00 through 2026-10-29 23:58 UTC; force-close at 23:59.
- 64/64 Linux unit tests passed; known three trades reproduce net -0.114092125013741 USDT.
- Baseline matches original commit 443ca56 exactly: 772 training and 572 holdout events, zero numerical discrepancy.
- Future OHLC/prefix invariance and holdout/training-selection independence tests passed.
- Accounting, position pairing, equity drawdown and dataset SHA-256 reconciliation passed.
- CSV SHA-256: `47d3be14ecdbf872f1b95121c61ca514d799283797d9b1493d14098c3a2cbb00`.

## Frozen choices

SMA slope threshold selected from 0, 0.5, 1 bps on TRAIN net PnL only. Both slope families selected 1 bps. Overall TRAIN winner: slope-only. No threshold or winner was reselected on holdout.

## Metrics

USDT, 10 USDT nominal per trade, 1x, 1000 USDT starting equity per period. Trades are completed OPEN/CLOSE pairs including boundary liquidation. Default costs 10/10 bps, zero slippage. Stress: 2 bps adverse slippage each side.

| Period | Variant | Trades | Net PnL | Max DD USDT | Max DD % | Fees USDT | Fees / net loss % |
|---|---|---:|---:|---:|---:|---:|---:|
| Train | baseline | 386 | -11.584407 | 12.723734 | 1.2709 | 7.712455 | 66.58 |
| Train | confirm | 379 | -11.378240 | 12.541052 | 1.2527 | 7.577100 | 66.59 |
| Train | opposite | 742 | -15.861777 | 17.506536 | 1.7478 | 14.829408 | 93.49 |
| Train | slope | 214 | -6.783891 | 9.702500 | 0.9674 | 4.275864 | 63.03 |
| Train | combined | 301 | -10.404147 | 11.524354 | 1.1512 | 6.016613 | 57.83 |
| Holdout | baseline | 286 | -10.963244 | 10.969351 | 1.0969 | 5.714752 | 52.13 |
| Holdout | confirm | 284 | -6.397893 | 7.257101 | 0.7251 | 5.675931 | 88.72 |
| Holdout | opposite | 560 | -16.763951 | 16.901641 | 1.6902 | 11.196331 | 66.79 |
| Holdout | slope | 190 | -7.213535 | 8.487621 | 0.8480 | 3.797727 | 52.65 |
| Holdout | combined | 256 | -7.352241 | 8.316837 | 0.8310 | 5.117682 | 69.61 |
| Holdout + 2 bps slip | baseline | 286 | -12.103774 | 12.109879 | 1.2110 | 5.713610 | 47.21 |
| Holdout + 2 bps slip | confirm | 284 | -7.531460 | 8.179605 | 0.8174 | 5.675260 | 75.35 |
| Holdout + 2 bps slip | opposite | 560 | -19.002468 | 19.140213 | 1.9140 | 11.196318 | 58.92 |
| Holdout + 2 bps slip | slope | 190 | -7.972397 | 9.079265 | 0.9072 | 3.797536 | 47.63 |
| Holdout + 2 bps slip | combined | 256 | -8.375219 | 9.078784 | 0.9074 | 5.117603 | 61.10 |

## Interpretation

All variants lost money in both periods. The train-selected slope filter improves holdout net from -10.963244 to -7.213535 USDT, but is not profitable. Confirmation-only has the smallest holdout loss; this is an observation, not an out-of-sample selection rule. Opposite reentry increases turnover (560 vs 286 holdout trades) and commissions (11.196331 vs 5.714752 USDT), with a worse net result.

No candidate is recommended for activation from this experiment. About 22.6 days of artificial Spot Testnet history, extreme price jumps, one split, fixed costs, minute-open execution and omitted funding/orderbook effects are insufficient to establish a robust strategy for real futures. The three October 30 trades are arithmetic fixtures only.

Drawdown includes open positions and estimated exit costs but is sampled only at minute opens. Fees/loss is an accounting ratio, not an estimate of causal loss share. No intrabar high/low-based fills are assumed. See RESEARCH.md for full definitions and commands.

## Evidence files

- research_data/btc-testnet-1m.csv and provenance.json
- research_results/comparison.json: all training candidates, frozen choices, holdout trade journals/equity and stress metrics
- research_results/metrics.csv and summary.json: derived tables and data-quality facts
- research_results/legacy-validation.json: original-code event comparison
- tests/test_replay.py: accounting and no-lookahead regression checks
