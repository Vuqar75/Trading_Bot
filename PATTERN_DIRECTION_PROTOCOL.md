# One frozen direction-normalization test

Registered before fitting this candidate. The preceding audit found a correct
export/feature pipeline, but the final RF regressor made no split on side and
only one explicit pattern-flag split. Its cached OOF net MSE did not beat a
past-only constant. Hypothesis: a direction-aligned representation reduces the
interaction burden of a fixed shallow forest. This is not a claim of alpha.

## Exactly one representation

Transform the existing float32 X, without reading labels or future bars:

* Multiply ret1/4/16/96, EMA9/20/50/200 gaps, hour_trend/hour_gap/fourhour_gap
  by side (+1 LONG, -1 SHORT).
* RSI becomes side*(RSI-0.5).
* Upper/lower wick exchange places for SHORT (opposing/supporting wick).
  close_location becomes 1-close_location for SHORT.
* Merge each long/short pattern pair into its max: engulf/pin/inside/break.
  Input cases already have an unambiguous side. Preserve side and all three
  asset indicators as controls for real asymmetry.
* ATR, ADX, absolute body, volume ratio, BB width and cyclic time unchanged.

Output: 30 columns, named in pattern_direction_features.FEATURES. No second
variant, feature selection, threshold sweep, seed search or parameter change.

## Inputs, models and comparison

Use only committed pattern_ai_results/training_examples.npz and the original
summary.json/predictions.json.gz. Check source SHA-256, unique symbol/time,
finite X, side/asset binding, exact fold row counts and purged label cutoffs.
Targets, fees/funding/sizing assumptions and rejection masks remain unchanged.

Six-month rolling training; original 16 monthly tests July2025..October2026.
Candidate classifier/regressor: RF64 trees, depth5, min_leaf40, random_state42,
n_jobs2; original defaults bootstrap=True, class_weight=None, squared_error /
gini, max_features=1.0 / sqrt respectively. No fitting on test labels. Reuse
original cached raw-RF predictions; do not repeat original training/backtests.
Same 50,628 paired test cases for candidate, raw RF, global past-only constant
and per-asset past-only constant. No new market/provider requests or VM work.

## Decision fixed before results

Forecast-quality checks (all required):

1. Candidate net MSE and Brier each improve at least 1% over the BEST aggregate
   reference among raw RF, global and per-asset constants.
2. No asset's net MSE or Brier worsens more than 2% versus its best reference.
3. Candidate net MSE beats the best monthly reference in at least 9/16 months.

Retain original candidate gate p>=0.60 AND predicted stress-net>=0.0005.
Report candidate counts/actual stress labels without claiming executed trades.
For an economic evidence screen, per asset greedily skip overlapping cases
until the accepted case's original label_end_ms (246min); never manufacture
actual fill/exit times. Require >=60 such cases, >=30 UTC days, >=10 per asset,
>=10 each direction, positive mean actual stress target overall AND each
asset, and positive mean after removing the best case. These are dependent
counterfactual diagnostics, NOT a portfolio replay or paper admission.

Only if both screens pass is there a reason for a separate execution-level
validation. No automatic model export, runtime replacement, threshold lowering,
paid-call enablement or trading activation. If either screen fails, reject
this representation as a useful trading candidate in its tested form; report
any limited forecast gain separately. Do not iterate another version here.

Save feature/fold/source receipts, paired predictions and readable results.
Data2024–2026 and prior experiments have already informed this project: no
independent confirmation, causal conclusion or significance claim. Overlapping
labels and correlated assets limit the effective sample size.
