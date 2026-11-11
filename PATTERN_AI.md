# Technical-pattern AI: implemented component and actual limits

Runtime candidate: `e62bef84bf26ea1383d855970c450ce3c50778bf`.

## What is now implemented

A causal closed15m technical engine computes RSI14,ADX14,ATR14,candle anatomy,
EMA9/20/50/200 distances,volume ratio,returns,BBwidth and last COMPLETED1h/4h
context. It detects directional engulfing,pinbars,inside-range breakouts and
20bar breakouts. These numerical flags are computed by code,not guessed from
an image or LLM prose.

A real trained RandomForest classifier estimates P(stress-net>0); a separate
RandomForest regressor estimates stress-net return. Models are64trees/depth5,
not hand-coded SMA labels. Weights export as181KBJSON; deployed inference needs
numpy/pandas and NOT sklearn,training code,backtest imports,a GPU or API key.
The current trained artifact uses May-Oct2026examples and expiresDec1. It was
createdNov11; its historical validity windows do NOT mean it was published or
actually traded earlier. Probability calibration is not established.

`research.pattern_ai_server` publishes a local analysis at most once per
closed15m bar,after60seconds maturation. There are no retries,orders or
trading-DB accesses. With explicit `--paid-existing-budget`, the SAME command
also calls the EXISTING `adaptive_regular.tick`: original server journal,
ownership,encrypted credential,one-writer lock,window rules,uncertainty
blocks,$0.25totalcap,6/day and<=3640microUSD/attempt are retained. No second
journal,lock reset or direct bypassing HTTP call exists in this adapter.
The paid question remains the existing4h regime question;15m pattern
information is supporting evidence,not an authorization to trade.

`adaptive_ai_executor.request` now includes a supplied,validated
`technical_context`. Venue,symbol,as-of and candle cutoff must match the
original bound snapshot and orders_enabled must beFalse. Old requests without
context retain their response schema and content. New requests retain the
legacy6-field JSON response schema,so durable response/usage/recovery logic
continues to work. GPT receives the calculated patterns/metrics and learned
estimates with Russian explanations requested. LLM prose can still be wrong;
the computed pattern panel is distinct from that prose and cannot grant orders.

The existing `/ai` menu gains **Теханализ и паттерны** (`/ai patterns`).
It reads a separate public `technical.json`; it never collects prices,
invokes a model or produces a financial job. It explicitly labels local ML
separately from GPT,shows detected patterns,RSI/ADX/ATR/volume and estimates,
and marks expired data stale. Unknown patterns or a trading grant invalidate
the panel. The existing privileged publisher validates the exact technical
snapshot and copies it0640for the tradingbot group; the bot cannot write it.
Existing GPT publication remains separate and unchanged in meaning.

## Completed experiment, not a profitable-system claim

Protocol fixed before calculation in `e483ccf`; experiment code in `e15e6eb`.
Same210hash-pinned public archive inputs as the preceding work; no new
historical-data sweep or old hourlyXGBoost rerun. All3assets reported.
69,515overlapping training counterfactuals,16monthly folds(Jul2025-Oct2026),
6monthtrain/purged4hlabel horizon. There are50,628chronological forecasts.
Past feature truncation invariance,fold cutoffs,and portable-tree/sklearn
prediction parity passed. These reused historical data are NOT independently
untouched OOS; overlap also prevents treating examples as independent trades.

The frozen entry gate was P(win)>=0.60 AND expectedstressnet>=5bp.
Maximum forecastP was0.5744907.224forecasts passed the net hurdle,but NONE
passed the probability hurdle. Therefore the initial AI policy executed
**zero trades**. This is a non-trading conservative policy,NOT evidence that
AI discovered profitable alpha or correctly rejected every losing trade.
No threshold,feature,asset or direction was changed after seeing this result.
Qualification isFalse in `research/pattern_ai_results/qualification.json`.

The execution baseline takes every identical pattern setup without ML,one
position per asset/scenario. It is a plumbing comparator,not a demonstrated
competitor. Its closed netUSDT from separate1000USDTaccounts:

| Asset | Baseline base | Baseline stress | AI base/stress |
|---|---:|---:|---:|
| BTC | −676.737641 | −925.166545 | 0 / 0,zero entries |
| ETH | −809.015475 | −903.332944 | 0 / 0,zero entries |
| SOL | −892.497842 | −902.517520 | 0 / 0,zero entries |

22,487baseline executed records passed independent gross/fee/impact/funding,
time,nonoverlap and risk/holding audits. They are NOT22,487AItrades.
69,515full counterfactual records remain locally reproducible; to avoid
committing a33MBduplicate JSON,exact model feature inputs and target arrays
are committed as7.38MBcompressedNPZ with source/hash receipts. Models,folds,
forecasts,execution journals,audit and qualification are retained.

Trading limits/stop/target/mark/funding are model assumptions as specified in
PATTERN_AI_PROTOCOL.md,not real fills or historical-filter certification.
FixedBTC0.001<=150notional,ETH/SOL<=100,1x,risk<=5apply only to research.
No working market size,pause,strategy or financial history was changed.

## Verification and live limitations

Final candidate passed **83targeted tests** covering technical features,
future/stale data,unknown patterns,legacy request compatibility,same-budget
mock execution/usage/idempotency,publication,existing AI paths and read-only
Telegram routing. NumericalRMA extraction is identical to the training helper.
Inference import isolation proves no sklearn/backtest module is imported.
Historical one-shot analysis peaked at63,484KiBRSS here; actualVM/systemd
resource behavior is not claimed. Existing-service candidate drop-ins provide
single-thread numeric libraries and128MBheadroom; actual paths/resources
must be checked before installation.

A current public USD-M capture was attempted ONCE in this cloud. The initial
`/fapi/v1/time` call returnedHTTP451; no retry,alternate-host/proxy workaround,
current quote or current GPT response followed. `gcloud` is absent;SSHagent
has no identities and defaultSSHkeys are absent;no VMconnector is available.
Thus this environment cannot perform the realVMdeployment/first provider
response. See `live_blocker.json`. Historical examples are explicitly marked
historical and cannot be published as current by the clock/freshness checks.

No paid provider request was sent. Existing key,original budget and VM were
not modified. The sole-journal integration was exercised with a mock only.
First real enriched GPT reply,usage,publisher and server backup must be checked
by the already connected local agent using PATTERN_AI_LOCAL_TASK.md.

## Running the implemented code

```
# Public capture only; no key,budget or trading operation.
python -B -m research.pattern_ai_capture BTCUSDT /tmp/candles.json.gz
# Read-only local model analysis; now-ms must be actual current time.
python -B -m research.pattern_ai_analyze --model research/pattern_ai_results/model.json --candles /tmp/candles.json.gz --now-ms ACTUAL_UTC_MS
# Build the EXISTING-executor-compatible request without sending it.
python -B -m research.pattern_ai_analyze --model research/pattern_ai_results/model.json --candles /tmp/candles.json.gz --now-ms ACTUAL_UTC_MS --llm-request
# Existing-server adapter defaults to local ML only.
python -B -m research.pattern_ai_server --model research/pattern_ai_results/model.json --technical-output EXISTING_PUBLIC_DIR/technical.json
```

Do not run historical examples with forged current timestamps or use an
independent budget to work around server access. Do not lower the gate merely
to create trades. This release implements analysis and its integration;
a profitable/admitted trading strategy remains unresolved.
