# One fixed causal correction of pooled net forecasts

Registered AFTER raw panel development failure, BEFORE this correction's PnL.
This is a development iteration, not validation of the failed models. Raw HGBR
base gross was +15.98USDT but its actual net was -24.39; stress gross+4.31 and
net-55.08. Ridge also failed. Do not change fees or declare these models passed.

Question: can past out-of-sample forecast errors conservatively suppress
systematic overprediction? Reuse ALL saved monthly HGBR/Ridge forecasts and
training outcomes without fitting or downloading anything. Only forecasts from
strictly earlier completed-label days may update correction. Future labels of
the current decision are never consulted.

Fixed correction separately per model and direction: previous30 UTC calendar
days of raw-positive (>=0.0005) forecasts, pooled across original five assets.
Each day's mean error is actual stress net return minus original prediction.
A whole day's errors become eligible only after NEXT midnight+246 minutes,
which is later than every four-hour label in that day. Require at least14
eligible days and100 forecasts. Correction=min(0,25th percentile of daily mean
errors), numpy linear percentile. It can only lower the prediction, never
increase it. This is an empirical penalty, NOT a calibrated probability or a
confidence bound. Daily dependence, regime changes and sparse tails remain.

Adjusted prediction = original prediction + correction; same positive-net
threshold+0.0005. Re-rank simultaneous candidates on ADJUSTED forecast, original
symbol/LONG tie ordering. Same4hstop2%,100cap,1x,1000sharedcapital, source funding,
base/stresscosts/delays. No backfilling before initial14-day qualification.
Jul-Dec2025 remains development; same counts/quarters/DD/besttrade/bestasset/
active-month criteria. Keep both models and all losing coins. No other penalty,
lookback, percentile or threshold tried. Only a passing fixed winner could
advance to2026 after publishing its result receipt. No live/paid action.
