# Maker viability: correction and fixed-path arithmetic

**Decision: insufficient data for the requested 48-hour rule; do not admit it.** The previously selected 96-hour path is also not admitted: its gross plus funding cannot cover even zero-extra-cost maker-entry/taker-exit fees on the saved stress path. No new collector, backtest, order or strategy was launched. The 48-hour version has no matching saved calculation; the results below must not be attributed to it.

## Exact saved rules and a correction

The prior `MAKER_ECONOMICS_20261111.md` incorrectly described the examined 1h A SHORT path as having a 48-hour maximum holding period. The original plan and `research/usdm_screen.py` instead set the time-exit threshold to `2 * 48 * 1h = 96h` for family A. Five of 219 BTC base trades and five of 229 stress trades have the saved `time` exit reason. When the threshold is reached, exit is scheduled through the next modeled 5m event; a previously pending exit can affect timing. Therefore 96h is an exit trigger, not a guaranteed latest execution time. The 48h rule changes the strategy and cannot be inferred from 96h aggregates.

The remaining rule is consistent with the saved 1h A SHORT code: after at least 96 closed 1h bars of warmup, take a **fresh transition** to a signal close strictly below the minimum of the previous 48 hourly closes; an ongoing below-minimum state does not repeatedly signal. Only one short position. Signal exit when the closed hour is strictly above the maximum of the previous 24 hourly closes. The stop is 0.75% above the model's actual adjusted entry price, checked against 5m trade OHLC, with mark-based liquidation checked first and adverse gap/stop treatment. Entry and signal/time exits are scheduled to the next 5m modeled open, with stress delayed one additional 5m bar; this is not a confirmed market fill. The script uses 1000 virtual USDT per segment, <=10 USDT notional, 1x; four segments start independently, and current BTC minNotional 50 USDT is not satisfied by this path. Three base and three stress funding/stop bars had ambiguous ordering and use the source's conservative funding convention. No liquidation or open marked position exists in the eight relevant summaries.

The source provenance hash `0cbb9ea292f33eebfdce984beb3d034e66208444c2e2730025e2d3f91df5457c` is reproduced from the four committed source files when `research/intraday_spot.py` is represented with CRLF and the other three with LF, reflecting checkout line endings. The published 3,360-row CSV contains **segment aggregates**, not individual trade records: `simulate` builds a local trade list but returns only totals, best old-net trade, counts and exit-reason counts. The local research checkouts and saved result artifacts inspected here contain no per-trade journal for these paths. Thus the exact identities and fees of individual skipped or filled maker trades cannot be recovered from the saved result. No prices or trade selection were fabricated and no historical simulation was repeated.

## Upper boundary on the saved 96h paths

Each row below uses the preserved saved path, without asserting that maker orders would fill or that changing entry/exit execution leaves subsequent stop decisions and trade selection unchanged. Saved gross is the reference-price move; funding is signed. Commission is hypothetically 2 bps maker entry and 5 bps taker exit. For these **all-short, closed, zero-liquidation** summaries, the old executed-fill entry and exit notionals are recoverable exactly from `turnover`, reference `gross` and saved `impact`: `entry=(turnover+gross-impact)/2`, `exit=(turnover-gross+impact)/2`. Fees are applied to these old notional amounts; they are not the unknown actual maker fill notionals. Reference gross less those fees with **zero further spread, impact or adverse selection** is an optimistic arithmetic boundary on this fixed path, not the maker strategy's forecast.

All bps below use `D = (saved entry + saved exit notional)/2`, not total turnover or the 1000-USDT account.

| Saved 96h scenario | Trades | Gross + funding | Maker entry fee | Taker exit fee | Remaining before other costs | Before costs, bps | Fees, bps | Remaining, bps |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Base | 219 | +1.732841 | 0.417754 | 1.044655 | **+0.270432 USDT** | 8.294918 | 7.000388 | **+1.294530** |
| Stress | 229 | +0.631694 | 0.436699 | 1.093700 | **−0.898704 USDT** | 2.890458 | 7.002681 | **−4.112223** |

Base `D=2089.038867 USDT`; stress `D=2185.446153`. The base path can therefore tolerate only **0.270432 USDT**, or **1.294530 bps on D**, of *total additional* spread, slippage and adverse selection before its modeled result reaches zero. This is not a per-side allowance. A further 0.5, 1.0 and 1.3 bps reduce the fixed-path base remainder to +0.165980, +0.061529 and −0.001143 USDT respectively. Stress is already negative before any additional cost. The original base and stress paths have different trades; comparing them is a sensitivity to old modeled delay/impact, not an actual maker stress simulation. Changing fees, order type, fill times, partial fills or size can change stops and later entries, so do not just deduct these costs from a supposed identical maker trade set.

## Sensitivity to which trades are absent

**The following are mathematical scenarios, not claimed maker-fill probabilities or decisions available at entry.** As an unbiased hypothetical benchmark, skipping exactly 22 of 219 saved base trades uniformly at random gives an expected remainder of +0.243266 USDT; skipping exactly 110 gives +0.134599 USDT. That expectation assumes random missingness independent of payoff and a fixed remaining trade list. It says nothing about dispersion or adverse selection; a maker order can fill precisely on less favorable movements. If one fill fraction is reported without its composition, its PnL cannot be determined.

To show the composition effect using sets that actually are retained in the source, retrospectively remove *all* trades of one recorded segment and retain the others. These are not executable selection rules and deliberately use information from after the trades:

| Base segment omitted | Trades omitted | Remaining fixed-path maker/taker boundary, USDT | Remaining bps on retained D |
|---|---:|---:|---:|
| 2025H1 | 60 | +0.661912 | +4.358407 |
| 2025H2 | 63 | +0.166259 | +1.112645 |
| 2026A | 46 | **−0.695846** | −4.227401 |
| 2026B | 50 | +0.678972 | +4.222177 |

Different omitted trade sets change the result's sign; dropping the 46 trades of 2026A removes a large share of the observed gross. The biggest individual trade saved by the old model is +1.442990 USDT **after the old taker model's own costs**; that old taker path's total net is −2.445237, or −3.888227 without this best trade. Those figures cannot be converted into exact per-trade maker sensitivity: the record does not preserve that trade's entry/exit notional, reference gross, funding or maker fill.

## Decision and unknowns

The 96h maker adaptation is **not admitted**: only its optimistic base arithmetic is positive, the saved stress path fails even before additional execution deterioration, the margin is small, the result depends strongly on the selected period, and its original BTC order size fails current filters. Unknown maker queue position, fill/cancellation timing, partial fills, adverse selection, exit depth and current/historical size constraints prevent a prospective PnL estimate. The requested 48h variant is **not evaluated** because no matching saved path or individual trade history exists; neither its profitability nor its loss is inferred. No market collection follows automatically from this report. The former proposed 48h protocol in `MAKER_ECONOMICS_20261111.md` was explicitly corrected; do not repeat this fixed-path accounting or represent it as a 48h backtest.

`python -B -m research.maker_viability` reproduces the numbers from the hash-pinned saved summary; `python -B -m unittest tests.test_maker_viability` covers exact SHORT notional reconstruction, ledger reconciliation, thinning arithmetic and the 96h/48h distinction. Full results are in `research/maker_viability_96h.json`. No common trade code changed; no application-wide suite was run. No VM, trading state, AI timer, account, budget, credential or reserve was changed, and no paid request was made.
