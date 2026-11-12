# Cost-targeted pooled ML: fixed offline development protocol

Registered before this model's fit/PnL. Motivation: prior fixed oscillators have
no demonstrated gross advantage. Earlier hourly ML predicted raw one-hour BTC/
ETH returns; older pattern RF used sparse 15m patterns on BTC/ETH/SOL. This
experiment instead pools the SAME fixed XRP/ADA/DOGE/LINK/DOT cohort, represents
common-market versus coin-specific hourly movement, and learns two directional
FOUR-hour stress NET execution outcomes. It is a new development experiment,
not a qualified replacement of installed ML. All earlier failures remain failures.

Inputs: existing pinned Dec2024-Dec2025 trade/mark/funding archives, no download,
no2026, no key/VM/paid request. 2025H2 is development, NOT untouched history.
January-June2025 supplies first training window. Six rolling six-month training
windows, July-December test months. Strict label-end purge before each month.
No fitting using test outcomes or later months. Both directions are labelled
ONLY in training; actual labels never influence inference or portfolio choices.

Closed1h features fixed: own log return1/4/24h; SMA20 gap; WilderATR14/close;
20h return std; RSI14; body/hour-range; close location; volume/previous20h mean;
cohort mean returns1/4/24h, residual return1/4h, cross-sectional return ranks1/24h;
most recent settled funding rate lagged15min and age; five asset indicators.
No current unclosed candle, price levels, future fund rate or future label input.

Two directional HistGradientBoostingRegressors, max_iter100, learning_rate.05,
max_leaf_nodes7,max_depth3,min_samples_leaf200,l2_regularization10,
early_stoppingFalse, random_state20261112, one CPU thread. Standardized Ridge
alpha100 is a separately executed control with identical features/labels.
No model/hyperparameter search. Pin sklearn/numpy versions and all feature names.
Predict stress net return, including delayed +6min entry, fee5bp and impact10bp
per side, actual intervening funding, fixed2% adverse-gap/stop-first stop and
4h timed market exit. Candidate signal requires predicted stress net>=+0.0005.

For EACH model one global1000USDT account/one position/pending action across
five symbols. Simultaneous signals ranked predicted net descending, lexical
symbol, LONG before SHORT on exact ties. No fallback or pyramiding. Base entry
+1min/impact5bp, stress+6min/impact10bp; fee5bp per side both. <=100notional,1x,
USD risk<=5; same coarse qty/min50 proxy as legacy policy, not verified filters.
Stop2%,time4h; all realized costs remain in shared cash. Training labels and
portfolio replay use this SAME stop/time contract; audits compare actual traded
stress records against precomputed outcomes, not just aggregate arithmetic.

Pass per model: >=30 closes and10 each side both scenarios; net>0 both;
stressQ3 andQ4>0; without best trade>0 AND without best asset>0; MTMDD<=2%;
>=3 active months. If either passes, predetermined selector higher stress net,
then lower DD, then lexical model name. Publish receipt BEFORE any2026 download;
only selected policy advances with unchanged rules to2026. No runner-up after
validation failure. If neither passes, keep forecasts and rejection; no tuning.

Checks: prefix feature invariance, monthly label purge, target cost arithmetic,
training-only standardization, one shared capital/position, quantity/risk,
forecast threshold/ranking, stress execution matching labels, saved funding and
source hashes, durable per-month model/checkpoint plus per-scenario journals.
Only affected targeted tests; no entire trading-suite repeats.

Research context reviewed 2026-11-12: Xue/Zhou2026 hourly return reversal
https://doi.org/10.1016/j.jedc.2026.105416 and prior cost-aware hourly ML
https://arxiv.org/abs/2606.00060. Neither establishes this pooled futures policy.
Bianchi/Babiak/Dickerson2022 liquidity paper reports particularly weak Binance
net results; do not transfer its broad-universe headline returns to this setup.
