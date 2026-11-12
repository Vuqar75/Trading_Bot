# Saved VM runtime audit and history evidence rollout

On 2026-11-12, the published snapshots and histories were copied read-only, together with the installed model, to `D:\TradingBot-ML-work\runtime-audit-20261112`. No exchange or model-provider request was made for this audit. Model content ID matched `2a0ecbe3351f44f36ce59c336b4e28d089841de21fa5ec915276117fe3d713bc`; installed model byte SHA-256 was `cc3ee2473b4ba5c24bd7f555b47acf4d32cdfe72f414aabc89c744bb7afa8277`. Distinct line endings explain the different byte SHA of the semantically identical source JSON.

Initial three published snapshots were WAIT/no_direction: each had full features but no numerical prediction to replay. Their historical records were ATR-only. While performing the work, an ordinary independently scheduled free cycle published directional SHORT snapshots for the 15:00 Baku boundary. A second read-only copy produced the following snapshot-only results, with no waiting for or triggering a new signal:

| Asset | Full actual predictions replayed | Maximum absolute error, probability / stress net | Old history with insufficient features | Other model |
| --- | ---: | ---: | ---: | ---: |
| BTC | 1 | 0 / 0 | 10 | 0 |
| ETH | 1 | 0 / 0 | 9 | 0 |
| SOL | 1 | 0 / 0 | 11 | 0 |

The actual three probabilities were 0.2360302588704465, 0.242192369452326 and 0.26210579001080503; stress-net was -0.0030340276459279256 for each. This is a check of three actual full vectors, not confirmation of the 30 ATR-only old vectors. Results, file hashes, initial WAIT checks and separately constructed local checks are in `research/pattern_ai_results/runtime_vm_*_20261112.json`. No old missing features were inferred or backfilled.

Installed only `research/technical_plan_history.py` from candidate `ef24f696240901d3ffc44adb5c122c468215917a` into the existing free writer at `/opt/tradingbot-multi-cafcee4-server/research/technical_plan_history.py`, normalized SHA-256 `2d3192c46e480bc8194e8b8ad3d54bd9629b2e1672a68a6f9eb7f9521b58e977`. The prior installed file matched the parent candidate. Installation was an atomic replace while the service was inactive under its existing writer lock; no timer, unit, publisher, bot or trading service was restarted. The existing free timer and publisher remain active, study and Donchian remain disabled. The model, thresholds and budget were not changed or accessed beyond the permitted model file.

Before replacement, a lock-consistent copy of all six technical snapshots/histories and the old module was obtained and restored/validated locally without a writer: `D:\TradingBot-ML-work\runtime-audit-20261112\locked-before.tar.gz`, SHA-256 `8d1d3639e7d443dfdb7468d6068c3c71fc12ff0db09efc8e9c7cf75bc86741d3`. In the subsequently published copies all prior plan IDs and the three started_ms values were preserved. The regular cycle added one plan per asset between those observations; it was not initiated by this work. Existing host-bound external backup and its cross-host/periodic limitations remain as documented in `AI_MULTI_ASSET_INSTALL_20261112.md`.

On isolated copies of the three genuine full directional snapshots, the new `advance` function preserved all 30 feature fields and the diagnostic reproduced both model outputs with zero error. A synthetic anchor candle was used solely to exercise the local history writer contract; it is not market evidence, and the generated histories were not put on the VM. The newly installed writer will retain the full original vector for future plans; a live post-install new-record event has not been asserted. No training, backtests or repeated full test suite were run. The 196 targeted checks and final 26 checks of the unchanged candidate were already documented in `MODEL_FLATNESS_AUDIT_20261112.md`.
