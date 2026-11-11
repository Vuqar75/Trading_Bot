# USD-M session momentum: fixed offline experiment

**Decision: reject this configuration for prospective paper observation.** Both assets lose after modeled costs in both scenarios, all four stress segments are negative for each asset, and BTC exceeds the predefined 2% closed-equity drawdown limit. Neither research trades nor the predicted associations are live orders. The fixed protocol was committed and published as `3b7a0290f0de9f068aef641dc73a2e9e61e1c8b7` before results.

## Source and adaptation

[Shen, Urquhart and Wang, Bitcoin intraday time-series momentum](https://centaur.reading.ac.uk/100181/3/21Sep2021Bitcoin%20Intraday%20Time-Series%20Momentum.R2.pdf), equations 1–5, 8 and table 8, is the primary source. It studies BTC/USD on five spot exchanges through 2020; Bitstamp's volume-defined session is 09:00–17:00 EST, and the prior close–09:30 return and 16:00–16:30 return have opposite sign for a trade in 16:30–17:00. We apply its joint-sign rule to Binance BTC/ETH USD-M, not its spot PnL. The paper labels time EST without a verified daylight-saving convention; we choose fixed UTC−05:00, with no claim that the authors handled DST that way. Session parameters, delays, expense assumptions, risk scenario, sample, account and tests were fixed in `SESSION_MOMENTUM_PROTOCOL.md`. Calendar 2025 is development; 2026-01 through 2026-09 is check. Both are previously used project data, not independent evidence. A position closing after a reporting boundary belongs to the session in which it was opened, with its full exit and costs.

The paper defines break-even as cost reducing the table-5 performance to zero; its table-8 combined-rule 10 bps and 0.096% per trading day are **not** interchangeable with our execution-adjusted account PnL. Our full-round-trip break-even denominator is total entry reference notional: 10000 × (gross + signed funding) / sum(entry reference notional), including both trade sides in the cost. If negative, even zero cost would not break even on that modeled path.

## Signal predictability before trading

On 638 daily observations per asset the article's one-predictor OLS (intercept and ONFH) gives BTC slope −0.0000356, R² 0.000000038; ETH slope −0.01519, R² 0.00440. The two-predictor OLS gives BTC ONFH 0.000972, SLH −0.19127, R² 0.05546; ETH ONFH −0.01473, SLH −0.20000, R² 0.04273. These are descriptive full-period coefficients, not author-equivalent pooled/Newey–West estimates, independent validation, or executable returns. Segment coefficients and counts are in `research/session_momentum_results/summary.json`.

## Execution results

USDT; each asset uses its own 1000 account. Gross is at reference minute opens, with adverse fill-price adjustment accounted once as friction; funding is signed and zero here because no recorded funding time falls inside these modeled holdings. Base taker 5 bps per side + 2 bps friction per side; stress 5 + 5 bps per side and longer delay. The [public Binance fee table](https://www.binance.com/en-BH/fee/futureFee) informed the assumed regular taker level, not the owner's verified rate or a historical fee schedule. Snapshot spread evidence covers only 30 minutes and is not a fill guarantee.

| Scenario / asset | Direction | Trades | Gross | Fee | Spread + impact | Funding | Net |
|---|---|---:|---:|---:|---:|---:|---:|
| Base BTC | LONG | 93 | 0.3377 | 7.3141 | 2.9257 | 0 | −9.9021 |
| Base BTC | SHORT | 116 | 0.4344 | 9.2473 | 3.6989 | 0 | −12.5119 |
| Base ETH | LONG | 142 | 2.1344 | 3.0472 | 1.2189 | 0 | −2.1317 |
| Base ETH | SHORT | 184 | 0.2892 | 3.9291 | 1.5716 | 0 | −5.2115 |
| Stress BTC | LONG | 93 | −2.3661 | 7.3137 | 7.3137 | 0 | −16.9935 |
| Stress BTC | SHORT | 116 | −1.5128 | 9.2479 | 9.2478 | 0 | −20.0085 |
| Stress ETH | LONG | 142 | 1.3158 | 3.0478 | 3.0478 | 0 | −4.7798 |
| Stress ETH | SHORT | 184 | 0.1221 | 3.9291 | 3.9291 | 0 | −7.7362 |

Combined separate accounts: base BTC net −22.4140, ETH −7.3432, sum **−29.7571**; stress BTC −37.0020, ETH −12.5160, sum **−49.5180**. No shared capital or portfolio netting is implied. Base mean net per trade is −0.10724 BTC and −0.02253 ETH; stress −0.17704 and −0.03839. Average modeled holding is 29 minutes base, 30 stress. Base full-round-trip break-even is 0.4662 bps BTC and 3.4746 ETH; stress is −2.3421 BTC and 2.0611 ETH. These are path-specific arithmetic sensitivities, not fill evidence.

| Asset / period | Base net | Stress net |
|---|---:|---:|
| BTC 2025 H1 | −8.2582 | −14.3191 |
| BTC 2025 H2 | −2.0631 | −4.0165 |
| BTC 2026 H1 | −7.4624 | −12.0396 |
| BTC 2026 Q3 | −4.6302 | −6.6268 |
| ETH 2025 H1 | −2.1697 | −4.0379 |
| ETH 2025 H2 | −1.7450 | −3.0065 |
| ETH 2026 H1 | −2.1163 | −3.6845 |
| ETH 2026 Q3 | −1.3122 | −1.7870 |

## Precommitted decision gates

* **Sample and coverage: pass as a research minimum.** Each asset has 638/638 evaluable session signals/targets; 209 BTC and 326 ETH modeled completed trades, with 133 and 132 respectively in 2026. Minimums were 100 full-period and 30 check per asset and >=90% coverage each segment. This does not prove statistical independence.
* **Frequency: pass numerically, with a practical caveat.** 331 BTC and 328 ETH signal days; mean signal gaps 1.92 and 1.94 days, maximum 9 and 11. BTC executed-entry gap reached 193 days due to modeled eligibility and cap, ETH 11 days; daily signals are not daily eligible fills.
* **Positive net base and stress on both assets and 2026: fail.** All are negative. BTC 2026 net base −12.0926 and stress −18.6664; ETH −3.4284 and −5.4716.
* **Three positive stress segments per asset: fail.** None of four is positive on either asset.
* **Without the best trade and without the better asset: fail.** Base BTC excluding best trade −23.1946, ETH −7.7085; stress −37.4873 and −13.0654. Removing the better ETH account leaves BTC base −22.4140 and stress −37.0020.
* **Drawdown <=2% of each starting account: fail for BTC.** BTC closed-equity max drawdown base 2.243%, stress 3.700%; ETH 0.748% and 1.262%. Intratrade drawdown is not measured, so these are lower-information closed-equity figures.
* **Model suitability remains limited.** No actual historical bid/ask, receipt timestamps, queue, partial fills or historical filter versions. The 1x liquidation buffer is only a conservative data-quality gate and a gap can exceed the model's 5 USDT scenario loss. The result does not authorize live or paper entries.

## Sources, omissions and reproducibility

The 132 required BTC/ETH USD-M trade/mark minute and funding monthly files were verified against previously saved SHA-256 values; immutable references are embedded in the summary and existing manifests. Temporary cache versions removed to recover local disk space may be re-read from the public URL only after the same hash check. The ETH 2026-06-29 mark gap blocks one model entry in each scenario; it was not synthesized. BTC rejected 115 entries for >100 USDT and 7 for the risk gate; ETH rejected one risk-gate entry and one missing entry mark in each scenario. None of the modeled positions is unresolved. The current public `exchangeInfo` filter subset and response hash are saved beside results; using these values for 2025–26 is expressly a proxy, not evidence of historical filters.

Run using the cache moved and hash-verified on 2026-11-11: `python -B -m research.session_momentum D:/TradingBot-reproducible-cache-20261111/session-momentum-cache research/session_momentum_results`. If mark cache is absent, only its original public archive URLs are read and every result is SHA-256 matched before parsing; no secret, paid API or order is involved. The custom session adapter reuses the project's archive parsing, funding arithmetic and existing adverse-fill accounting convention; the earlier engine's fixed strategy exits/stops do not implement equation (8), so its incompatible stop is not applied. The compact compressed journal contains daily signal, model availability, rejections and completed fills. `python -B -m unittest tests.test_session_momentum` passed 9 tests for boundary, side, no future-target dependency, fee arithmetic, funding, missing data, cap and archive hashing. A full repeat matched the journal and summary byte-for-byte after the conservative mark-buffer refinement: uncompressed journal SHA-256 `2edb7c4197052ac995fb462ece5ea72180de33062bd7415179d5fada4b3484af`; summary SHA-256 `390e5ba3d070184d863d17ed64b0a0c1d81f94ffe8d1a313b6a0824e1b4fb0b8`. No full application suite was run.

No code was installed, no automatic inputs enabled and no VM/trading settings changed. Paid AI/API calls: $0. Ordinary VM and network costs are not inferred from that statement.
