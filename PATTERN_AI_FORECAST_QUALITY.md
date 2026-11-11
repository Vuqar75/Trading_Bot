# Local ML forecast-quality audit after the successful server rollout

## Operational context accepted

The owner and published rollout commit339c522 report successful VM deployment
and first enriched GPT response on2026-11-11 at16:02:43UTC. GPT and its
same-snapshot baseline both choseRANGE. The sole journal has8attempts and
18,370microUSDreserved; later paid boundaries are disabled. The local technical
updates continue and all4workingmarkets were reported paused. These are
rollout receipts,not a fresh VM check by this cloud. No repeated owner screen
confirmation or extra paid attempt was requested.

This audit addresses a DIFFERENT question: whether the local RandomForest
actually ranks profitable pattern entries. It does not grade the single GPT
regime answer or assert that GPT is unhelpful.

## Result: the probability threshold is not the only problem

Read the existing50,628chronological forecasts and their original stress
counterfactual targets. No training,signal change,threshold search or replay.
The regressor component of the ORIGINAL frozen gate,predicted stress-net>=5bp,
selects224examples across58UTCdays and9testmonths. This was not a new entry
policy: original admission also requiredP(win)>=60%,so actualAItradesremain0.

| Mean per selected counterfactual | b.p. of entry notional | Percent |
|---|---:|---:|
| Predicted net after stress costs | **+16.4968** | +0.164968% |
| Actual reference-price gross | −8.5920 | −0.085920% |
| Commission | −10.0073 | −0.100073% |
| Modeled spread/impact | −20.0145 | −0.200145% |
| Funding | +0.1775 | +0.001775% |
| Actual net | **−38.4362** | −0.384362% |

Actual positive fraction is35.71%; mean predicted probability was41.85%.
These are overlapping counterfactuals,NOT224executed trades. No aggregate
portfolio cash loss is inferred by summing them. They share regimes/training
windows;224is not an independent sample size. No significance/CI claim.

All selected-asset conditional means are negative:

| Asset | Examples | Predicted mean net,bp | Actual mean net,bp |
|---|---:|---:|---:|
| BTC | 23 | +11.3603 | −66.3115 |
| ETH | 72 | +14.7691 | −60.7815 |
| SOL | 129 | +18.3769 | −20.9944 |

Overall,across ALLforecasts,predicted mean−30.2249bp is close to actual
−30.5500bp. That agreement is insufficient: the positive-forecast tail,the
region required for trading,does not corroborate. The aggregate-average
accuracy must not be presented as useful profitable-entry prediction.

The selected reference gross is negative even before modeled commission and
impact. Lowering the probability gate,increasing size or merely assigning
more frequent GPT regime requests has no demonstrated repair from this audit.
No parameter was changed and no trading admission is granted. This does not
prove that a different model,information set or market strategy cannot work.

## Evidence and reproducibility

`tools/audit_pattern_ai_forecast_quality.py`:

- joins each forecast uniquely to its original asset/timestamp target;
- checks monthly test membership and purged training-label cutoff;
- uses exactly the original5bpregressor hurdle;
- verifies selected cost/gross/net against committed exact target arrays;
- extracts224selected raw outcome cases from the original hash-verified
  counterfactual archive; a small gzipcasefile is committed for reproduction
  without the ignored33MBduplicate;
- records sourceSHA256,per-asset/per-month decomposition and sample dates.

Artifacts: `research/pattern_ai_results/forecast_quality.json` and
`forecast_quality_cases.json.gz`. Reproduce by running the script; it uses
cached arrays and has no network,model training or paid API calls. No test
suite rerun was needed: production code/financial behavior did not change.

## Operating implication

AI technical analysis is now genuinely installed according to the verified
rollout. Treat that integration milestone as complete. Keep the local analysis
available and the recorded trading/paid-boundary restrictions intact. The
current local ML model remains an unqualified research model,not a profitable
selector. GPT'sRANGE is a regime assessment,not a range-strategy entry.

The next strategy effort needs an independently justified predictive input or
hypothesis rather than a relaxed gate on this failed positive tail. No new
collector,standing paid schedule,VMchange or automatic trading job is created
by this audit. Current cloud did not access the server budget or credential.
