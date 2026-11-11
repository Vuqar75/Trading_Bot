# Published EMA results: independent hourly replay, 11 November 2026

Decision: an inspectable historical positive result exists. It justifies a
bounded transfer experiment, **not strategy admission or an order**.

## Provenance

- Paper: Mroziewicz and Ślepaczuk, [arXiv:2602.10785v1](https://arxiv.org/html/2602.10785v1).
- Author engine: <https://github.com/tmr-crypto/wf_optim_crypto>, commit
  `8be3ecf0019e991962f6f59bf9938cfa56411350`.
- Author analysis: <https://github.com/tmr-crypto/wf_optim_crypto_analysis>, commit
  `a9b4d8d09da162c970253c0b8cb5e5b3f2400444`.
- The analysis README links the public file
  `https://drive.google.com/file/d/1y7J3cGFEYYBufVTACzPYr4GXiUSCOKm7/view`.
  Download succeeded without account credentials. ZIP SHA-256:
  `2bb40a7860733ba073a089c82b3424f4551d101d061fb31f4e9f51da978a81fd`.
  ZIP has 104 entries and 83,646,020 expanded bytes. Required artifacts were
  read; no downloaded R code was executed.

## What actually matched

`tools/review_published_ema_archive.py` independently reconstructs all 11 EMAs
(TTR-style SMA initialization), 30 fast/slow position series with one-bar lag,
signed-log returns less turnover costs, each training-only Sharpe selection,
and stitched test paths. Prices begin at saved `02_downsampled.rds`, not raw
minute source data. Numerical checks match saved `03`, `04`, `05`, `08`, `09`
artifacts. Clipped/full Sharpe matches `metrics.yaml` within 1e-5. Eight paths
cover 129,504 test rows **summed across overlapping paths**, not unique bars.

Original schedules: 14/10 and 7/28 training/test days, hourly bars. The 30-pair
selection is the author's algorithm, not a new outer optimization. No 81-window
screening or bootstrap was rerun.

| Author held-out path | Saved Sharpe | Actual stitched-position cost Sharpe |
|---|---:|---:|
| BTC 14/10 | 1.106450 | 1.096939 |
| ETH 14/10 | 1.337110 | 1.330651 |
| BNB 14/10 | 1.198157 | 1.194372 |
| BTC 7/28 | 0.830707 | 0.825258 |
| ETH 7/28 | 1.208476 | 1.206332 |
| BNB 7/28 | 1.099677 | 1.099677 |

## Correction and limits

Original code charges each EMA pair's counterfactual changes before stitching
selected pairs. At a selection boundary those costs can differ from the actual
exposure transition. Correction uses `0.001 * abs(p[t]-p[t-1])`, initially flat.
All detected mismatches occur at window boundaries. Initial entry is charged;
additional final closing cost 0.001 per full exposure is reported separately,
not included in table Sharpe. Positive means survive this limited correction.

The correction retains signed-log returns: it is **not** exact fixed-quantity
USDT-M PnL, funding or margin accounting. Historical aggregated Kaggle prices
are not confirmed executable Binance quotes. Initial backward filling in input
code requires an impact check if preprocessing is reproduced; its presence
alone does not show that these profits are caused by leakage.

Raw minute source and downsampling were not regenerated. No real fills, funding,
current filters, stops, latency or holding-time suitability are established.
The held-out period is 2019–2021, not independent evidence for current markets.
Reproducing a number is not proof of robustness or profitability for the owner.

## Reproduction

Research dependencies used: Python 3.12; numpy 2.3.5, pandas 2.2.3,
pyreadr 0.5.9, narwhals 2.28.0, rdata 1.1.0, xarray 2026.9.0.
An isolated Python environment suffices; no R/DVC/LaTeX installation is needed.

```
python tools/review_published_ema_archive.py /path/to/wf_optim_crypto_analysis.zip \
  --output review.json
```

The script verifies ZIP hash, asserts turnover/reversal arithmetic, and checks
every replay and metric match. Results: `research/published_ema_review/results.json`.
Large third-party files and source were not added to Trading_Bot Git.

No VM, Telegram, financial DB, AI request, paid resource, working pause, secret,
budget or live process was accessed. Next task: `PUBLISHED_EMA_LOCAL_TASK.md`.
Do not repeat this review unless an artifact changes or a mismatch requires it.
