# RSI15m protected execution verification — 11 November 2026

Decision: **reject this fixed adaptation for paper admission**. Its positive
base result does not survive registered stress. No parameter was tuned after
calculation; this does not establish that all RSI strategies are unprofitable.

## Result

BTCUSDT USD-M, calendar 2025, separate 1000-USDT accounts, notional at most
100 USDT at 1x. December 2024 is warmup. All positions are closed at the end.

| Metric | Base | Stress |
|---|---:|---:|
| Closed trades (LONG / SHORT) | 42 (12 / 30) | 40 (12 / 28) |
| Gross USDT, before modeled costs | 15.708673 | 6.996587 |
| Fees USDT | 3.730702 | 3.567438 |
| Spread/impact USDT | 3.730706 | 7.134883 |
| Funding USDT | +0.173287 | +0.186745 |
| Net USDT | **+8.420553** | **−3.518989** |
| Net LONG / SHORT USDT | −0.642788 / +9.063341 | −1.170392 / −2.348597 |
| Modeled MTM maximum drawdown | 0.7220% | 1.0706% |
| Median holding hours | 6.18 | 6.27 |
| Closed within 24 hours | 80.95% | 80.00% |
| Entry rejections: minimum quantity / cap | 52 | 52 |

Stress raises impact and entry delay; it changes the execution path and trade
selection. Its lower gross cannot be explained solely by subtracting larger
fees from the base trades. Both scenarios use 5-bp taker fee per side; combined
spread/impact is 5 bp base / 10 bp stress per side. Entry delay is one / six
minutes after the closed signal. Funding uses saved historical events.

Stress quarterly net: Q1 −2.628971, Q2 +2.096937, Q3 0 (no closed trades),
Q4 −2.986955 USDT. Only one quarter is positive, versus the required three.
Stress without its best trade is −5.463794 USDT. Minimum trade counts and
2% drawdown limit pass, but both-scenario positive net, stress robustness and
90% of trades within 24 hours fail. No admission or installation follows.

## Source and scope

Source [Dnayaka/btc-rsi-momentum](https://github.com/Dnayaka/btc-rsi-momentum),
commit `3216d14f111d705bd48b3b1746bd7cbaa2832653`, v20 default rules. Source
Pine SHA256 and full signal/execution specification are in
`RSI15M_VERIFICATION_PROTOCOL.md`, published in `c8f16a4` before acquisition
and calculation. Implementation and 39 archive checksums were pinned in
`43bcada` before the full calculation. No alternate version, timeframe or asset
was calculated. Author return claims were not independently reproduced.

This is a protected execution **adaptation**. Protection starts immediately
at modeled entry; default Pine recalculation can install the original bracket
later. TP is costed as taker, conservatively relative to a resting limit.
The 2.5% trailing activation is above both default profit targets and is
therefore dormant here. Macro windows use literal source-code rules, not a
certified calendar of actual releases. See protocol for exact asymmetries,
pullback states, stop and target percentages and cooldown.

All 39 public Binance archives (trade 1m, mark 1m, funding; December 2024 plus
2025) passed official SHA256 checks. No missing/conflicting input was accepted.
The source manifest, complete raw signal list, admitted actions, trade journals
and summaries are saved in `research/rsi15m_verification_results/`.

2025 has already been used by this project and described by the author. It is
not independent out-of-sample evidence. Current size-filter proxies are not
verified historical exchange filters. Minute OHLC cannot establish intraminute
paths, queue position, actual fills or gap execution. Same-minute bracket
ambiguity takes the adverse stop first. MTM drawdown retains the existing
conservative research assumptions. Costs are scenarios, not historical fills.

## Verification and reproduction

Five implementation checks cover RMA seed, future truncation invariance,
same-minute adverse bracket priority, short target/both fees and cash
reconciliation. An independent output audit covers all 82 closed trades:
reference-price gross, fees, impact/funding net reconciliation, funding sums
and event bounds, signal delay, no overlapping positions, quantity/notional/risk
limits and summary totals. Both audits pass; no residual position remains.

```
python -B -m research.rsi15m_verification /path/to/archive-root research/rsi15m_verification_results
python -B tools/audit_rsi15m_results.py
```

The market-data calculation was run once; arithmetic audit does not rerun it.
No production code changed, so the application suite was not rerun. No VM,
Telegram, working service, pause, budget, secret or AI schedule was accessed or
changed. Public historical archive downloads only; no paid API requests.
