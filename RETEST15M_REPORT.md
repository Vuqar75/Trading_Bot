# Small-account trend-retest adaptation — result

Decision: reject this fixed rule. BTCUSDT USD-M2025, separate1000USDT
accounts,notional<=100,1x. Source provenance and all adaptations were frozen
before the result in `85cc664`; see RETEST15M_PROTOCOL.md. No parameters were
changed after calculation. This is not replication of the author's portfolio.

| Metric | Base | Stress |
|---|---:|---:|
| Closed positions | 43 | 43 |
| LONG / SHORT | 7 / 36 | 7 / 36 |
| Gross USDT | −1.771567 | −2.074359 |
| Fees USDT | 3.862162 | 3.861766 |
| Spread/impact USDT | 3.862162 | 7.723529 |
| Funding USDT | +0.075617 | +0.091891 |
| Net USDT | **−9.420274** | **−13.567763** |
| Model MTM maximum drawdown | 1.4828% | 1.8855% |
| Closed within24h | 97.67% | 95.35% |
| Entry rejections min quantity/cap | 56 | 56 |

Stress Q1/Q2/Q3/Q4 net is−0.795577/−1.978599/0/−10.793587USDT. No open
position remains. Unlike the prior RSI failure, this rule loses at reference
prices before fees,impact or funding. Execution optimization alone does not
repair the observed gross result. LONG count and net/robustness criteria fail.
The timing criterion passes; that does not offset economic failure.

## What was tested

One position without pyramiding,15m EMA9/15 impulse then EMA-channel retest,
last completed4h EMA100/200 direction/slope,ADX>=20 and2x-volume crossover.
Stop1%,full target1.5%,time exit after100bars with modeled1/6minute delay.
Unlike source, full target replaces partial+ATR trailing: half the minimum
0.001BTC would violate the0.001step under this small notional cap. No source
profitability claim is transferred to this adaptation.

The source at PravarP11/multi-timeframe-trend-retest-strategy commit
f750f2ba12a83ace5de0f2e6d056bfcdaa6fe171 also returns a Wilder *sum* as ATR
rather than its mean; thus its nominal2ATR trail is14times a conventional
2ATR trail. It sizes whole-coin minimum1, not our fractional BTC filter.
Its current-universe screening overlaps later OOS period selection. These
findings mean the headline PF is not reliable evidence for our configuration;
they do not establish a corrected full source portfolio's profitability.

This test uses the already hash-pinned39 archives, including volume from the
trade ZIPs, without new downloads. Features passed a future-truncation check.
All86 saved trades passed a separate journal audit: reference-price gross,
net decomposition, entry/exit fees, funding sums, signal delay, nonoverlap,
notional/risk constraints and summary totals. Cash reconciles and there is no
residual. Exact receipts and trade/action/raw-signal journals are under
`research/retest15m_results/`. Results and audit were run once; no parameter
sweep, alternate asset or timeframe and no full application-suite rerun.

Reproduction:
```
python -B -m research.retest15m /path/to/archive-root research/retest15m_results
```

## Limits and operational scope

2025 is known development history, not independent OOS. Size filters are current
proxies, not proven historical filters. Bracket fills and adverse-first
same-minute ordering are OHLC execution assumptions, not real liquidity.
Protected immediately after modeled fill; source partial/pyramiding omitted
explicitly. Full goal of profitable short-term LONG/SHORT bot remains unresolved.
No VM, working code, service, pause, secret, budget or paid API was accessed or
changed. No local-agent task or collector was created for the failed variant.
