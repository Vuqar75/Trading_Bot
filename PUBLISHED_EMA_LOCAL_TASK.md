# Local continuation: published EMA on current USD-M history

Continue Trading_Bot from latest AGENTS/HANDOFF. Integrate
`research/published-ema-replication` without discarding local work or force push.
Read `PUBLISHED_EMA_REPLICATION_REVIEW.md`. The eight-path author replay is
complete; do not repeat it, rejected experiments, or operator test trades.

1. Before calculation, commit a transfer protocol. BTC/ETH USD-M hourly bars,
   authors' fixed 14d/10d and 7d/28d schedules and 30 EMA combinations only.
   No new outer search, timeframe, indicators or discretionary winner choice.
   Training-only 30-pair selection is explicitly authorized as the algorithm.
   Use Jan 2025–Oct 2026 plus existing warmup on D:. This history is known,
   not independent evidence.
2. Independently implement the pinned procedure: EMA seed, timestamp meaning,
   training cutoff, warmup, lag, training Sharpe, ties, step, calendar clipping.
   State code/paper differences. No backward filling. Check one small saved
   author selection example; no full historical replay or raw 830MB download.
3. Modern result A: author's signed-log diagnostic, with corrected costs for
   actual stitched exposure transitions. B: existing simulator with real
   fixed-quantity arithmetic, initial/final execution, two-sided reversal fees,
   funding, capital and filters. Do not treat signed-log short returns as USDT
   settlement. Keep positions/cash continuous across window and parameter changes.
4. For B: 1000 virtual USDT per separate asset account, notional <=100 USDT,
   1x, one position, 2% protective stop, estimated entry risk with costs <=5 USDT.
   After stop stay flat until a later hourly signal; define exact re-entry rule
   before results. This stop is our adaptation, not an original reproduction.
   No extra time exit/target. Measure holding times: hourly signal is not proof
   of an intraday strategy. Apply rounding and filters; record rejected entries.
5. B base: 5bps fee + 5bps combined spread/impact per side, 60-second modeled
   entry delay. Stress: 5bps fee + 10bps spread/impact per side, additional five
   minutes entry delay. No double-counted spread. Funding and minute stop gaps
   accounted conservatively. These are scenarios, not known historical fills.
   Select EMA pairs using base training costs only; keep same selected pairs in
   stress. Before calculation state whether selection uses original author
   training returns or stop-aware returns. Use original author training returns
   for this transfer, so the stop adaptation cannot change parameter selection.
6. Commit admission criteria before results, per schedule separately:
   >=30 closed trades/asset, >=10 each direction; both assets positive base and
   stress; >=3 of four predeclared combined calendar segments positive stress;
   combined stress positive without best trade; MTM drawdown <=2% per account.
   Report median/p90/max holding and share <=24h. If under 90% close within
   24h, it is not a short-term candidate for the owner's requested use. Report
   both schedules, no post-result adjustments or choosing by the test winner.
7. Save pair choices with train/test times, signals, full trade ledger, rejects,
   final unrealized residual, side/asset/segment net, gross/cost/funding and
   cash/same-size buy-and-hold benchmarks. Distinguish failed reproduction,
   failed modern transfer and an adaptation losing the author's advantage.
   Only targeted temporal/accounting checks, unless common trading code changes.

Finish protocol, calculation and decision in one pass. If both schedules fail,
close this method, no new sweep. If one qualifies, prepare a versioned candidate
for existing isolated paper contour. Do not install or activate trading here.

Leave VM on, working markets paused, research/Donchian/paid AI off. No real or
Testnet orders, paid API, resources, budget/credential change or history deletion.
Use D: for dependencies, raw files and caches; avoid large writes on C:.
Save results and HANDOFF in one functional branch and push normally. Final:
both decisions, economic table, holding times, limitations, branch and commit.
