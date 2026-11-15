# Protocol: shortlist target ranking audit

Scope fixed before calculation: the four already audited post-SHORT-fix BTCUSDT snapshots at 15 November 2026 08:02,12:02,16:02,20:02 Asia/Baku. No new snapshots, prices, outcomes or model requests.

Question: among mechanically admissible alternatives with identical direction, entry and stop, does descending net-RR ranking omit a closer target while including a farther target? Reconstruct the complete passing pool with the existing context/validate code. Compare it with the actually transmitted four-per-side shortlist. Report counts by slice/direction and unique entry, not independent signals.

For each transmitted candidate, count unique confirmed opposite-level prices strictly between entry and its padded target. This is a mechanical crossing metric, not proof that every level remained economically relevant. Check whether the nearest raw target fails existing validation; distinguish policy/risk rejection from ranking exclusion. No replacement ranking, parameter grid or trading simulation. Model WAIT reasons may support consistency but cannot prove how a counterfactual model would respond.

Decision: if a nearer valid omitted target exists for the same entry/stop while farther one is offered, the ranking bias is demonstrated. Otherwise reject that specific explanation. Do not infer profitability, modify runtime, install or call the model from this audit. Use exact saved clocks/candles and original fixed fees/risk/filter checks.
