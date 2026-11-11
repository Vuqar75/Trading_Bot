# Exchange-feasible fixed-BTC size check

The<=100USDT cap rejected52RSI and56retest signals in the completed2025 runs;
BTC minimum0.001 is infeasible above100000USDT. This is a size feasibility
constraint, not a missing trading signal or a demonstrated strategy defect.
Owner previously permitted reviewing isolated research limits when necessary.
No working VM/risk setting is changed.

Before results, fix one alternate sizing rule: exactly0.001BTC, never add or
increase leverage, entry notional<=150USDT,1x,existing<=5USDT modeled risk.
150is a round exchange-feasibility ceiling, not a profit-optimized sweep.
Everything else remains frozen,including sources/stop/target/time exits,
signal state,costs,funding and1/6minute execution delay. Test the two existing
fixed candidates RSI15m and small-account retest on2025,base/stress: four cells,
no alternate cap or parameter search. Retain original failed100-cap results.
This is a post-result diagnostic of size censoring,not independent discovery.

Apply original qualification metrics unchanged. If a candidate passes all
original2025 criteria, continue with fixed rules and150cap onJan-Oct2026,
no intervening tuning. Do not call2026independentOOS: it was used elsewhere
and source authors may have used it. Any final admission needs prospective
execution evidence; no automatic strategy activation follows this check.
If none passes, do not download further archives for them. Public historical
reads only; no secret,budget,paidAI,VM,workingpause or service access.
