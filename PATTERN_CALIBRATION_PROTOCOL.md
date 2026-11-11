# Chronological calibration of the existing pattern AI

Registered before calculating this calibration experiment. No new forest
training,features,threshold sweep,market API,paid GPT call or VM change.
Original model/artifacts and failed gates remain intact.

Problem established in bcbb05d: positive raw-regressor forecasts were
optimistic,although the unconditional average looked accurate. Improvement
must be measured on later months,not by lowering the60%probability gate.

Reuse original out-of-fold forecasts and exact stress-net targets. For each
asset and test month Oct2025-Oct2026,calibration uses ONLY previous3months'
out-of-fold forecasts whose maximum4h6mlabel end precedes the month boundary.
Fit monotonic isotonic maps separately:rawp->observedwin,rawnet->actualnet.
EachUTCday gets equal total fitting weight to reduce overlapping intraday
examples' influence. No fitted forest or test label is used as calibration
training input. Quantile edges for10supportcells are determined on calibration
scores only. Minimum200examples overall;percell>=100examples and>=20UTCdays.

New RESEARCH policy: calibrated expected stress-net>=5bp AND supported
calibration cell AND (mean daily cell net−2standarderrors)>0. Raw/calibrated
P(win) is informational,not an arbitrary win-rate gate:payoffs are variable.
The uncertainty score is a heuristic,NOT a guaranteed95%confidence bound;
returns can remain serially dependent acrossdays. Out-of-calibration score
ranges and missing/stale/mismatched calibrators produceNO_TRADE.
This is a separate versioned policy;original0.60/5bp policy stays unchanged.

Primary analysis-promotion criteria: pooled test Brier and net-returnMSE each
improve at least1% relative to raw;noasset's either metric worsens by>2%.
Report allasset/monthmetrics and candidatecounts/net without treating
counterfactual outcomes as portfolioPnL. No candidates does not provealpha.
Even on analysis-promotion success,orders_enabled/trade_allowed remainFalse
and existing trading eligibility is unchanged. Trade admission requires an
additional economic/execution test;this experiment cannot grant it.

Final companion:Aug-OctOOFcalibration with labels purged beforeNov1,bind to
existing November model ID,valid onlyNov1-Dec1. JSON portable,piecewise-linear,
no sklearn required onVM. Runtime integration optional/defaultoff and permitted
only if the above analysis-quality criteria pass. Preserve currentserver
single-paid-boundary guard,original journal/locks,allmarketpauses.

Historical sources/model family have already been examined. These are causal
rolling tests,not independent unseen strategy discovery or actual earlier
publishedforecasts. Save raw/calibratedforecasts,foldreceipts,qualitymetrics
and supportdiagnostics. Do not claim a profitablebot merely from better scores.
