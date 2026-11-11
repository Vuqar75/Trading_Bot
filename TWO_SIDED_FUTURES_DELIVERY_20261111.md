# Delivery of two-sided futures research code — 2026-11-11

The checked candidate is `9866a35d8a4f727d4d1483e9e3683bcc89edece2`.
Local integration merge `ceb2ded9b96bbf0a589696ba67a05d54c62cd28d` has the
same Git tree (`e384087010c226eeb9bcff34457cc87bd5640a91`) as that candidate;
the already completed Linux suite and offline bench were not rerun.

A staged release was delivered to `/opt/adaptive-two-sided-9866a35` on the
existing VM. It was based on the existing `/opt/adaptive-operator-7d433e6`
research checkout. Before copying each of the ten changed runtime files,
its normalized SHA-256 was checked against the exact `aa9cde2` base; after
delivery its SHA-256 was checked against `9866a35`. The per-file base and
candidate hashes are in `TWO_SIDED_RUNTIME_MANIFEST.json` in the staged
release. Existing directories, unit files and symlinks were not switched.
No live financial database or model was included in the release.

As `adaptivestudy`, an offline read-only smoke check verified that the staged
supervisor accepts `--route-policy isolated-futures-two-sided-v2`: TREND_UP
routes to isolated futures LONG, TREND_DOWN to SHORT, RANGE and NO_TRADE are
blocked. The default remains `isolated-router-policy-v1`. The smoke check
did not run a worker, create a job, access a trading database or use network.
These routing examples explicitly used `research_sandbox=True` and eligibility
for validation; they are not live trading decisions.

Before delivery, the existing study snapshot and external upload/isolated
restore verification passed at 19:21 UTC with SHA-256
`da9e28fd74879e5a3f0891a860f83831e3973f8fe196d959418f520a4139ff63`.
After delivery all four working markets were paused and their existing
processes active; `adaptive-study.service` and `donchian-observer.service`
were inactive/disabled. The prior read-only study-display and AI-analysis
timers remain active; paid AI remains restricted to the already-used boundary.
The single budget still has eight attempts and 18,370 micro-USD reserved.
No schedules, credentials, ledger, locks, capital, positions or pauses were
changed. `/opt/adaptive-study-current` still points to the existing
`/opt/adaptive-usdm-continue-018262d`; the staged release is not activated.

The trend1 strategy remains economically unqualified. Prepared execution is
not authorization to enable entries, and no live/test order or artificial
paper trade was created. Activation, if separately authorized in the future,
must first recheck the full live state, financial reconciliation, backups and
appropriate service rollout; simply starting the staged supervisor is not a
safe activation procedure.

Direct Git push of the saved merge was attempted without force and rejected
HTTP 403: Git Credential Manager authenticated as `ntagiyeva`, which lacks
write permission to `Vuqar75/Trading_Bot`. The exact local merge and its
history remain intact. The already-published candidate has the identical code
tree; publication through the GitHub connector preserves the documentation
but does not make the original local merge SHA an ancestor.
