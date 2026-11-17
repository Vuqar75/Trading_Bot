# b36103c normal paper runtime — installed17November2026

Completed the finite local rollout in PAPER_EXECUTION_RELEASE.md. Existing
D: checkout research/ai-limit-brackets was clean, fetched/fast-forwarded from
e3b4878 to b36103c9e582caa4813d2d0b9c69d1f4c7019743; no local work discarded.
[VM evidence](research/PAPER_EXECUTION_INSTALL_20261117.json).

## Installed result

**Actual runtime b36103c9e582caa4813d2d0b9c69d1f4c7019743**, started
**17November14:44:44.868Asia/Baku**, source confirmed14:44:59.390,
PID548045, /opt/tradingbot-ai-brackets-b36103c-paper. Canonical runtime SHA256
be52d3ccbd291295bd7beaa104a0dda9d3d8e3f38b2939b2276ee5fed16f06e2,
exactly matches the published manifest. Only research/ai_limit_runtime.py differs
from the retained previous paper deployment. All other non-cache file contents
and permission modes compared equal; no analyst/UI replacement or DB migration.
The optional demo was not installed/run and no package added.

Ordinary serve is running WITHOUT --phase-diagnostics; unit/cmdline checked.
The old1,806,583B private phase file was preserved byte-for-byte and its mtime
remained frozen across ordinary operation and the external backup. SHA
b1a0955bd2a9ddbf5b3f14e0445c2abc4cc744da1d6ff0ddd1e469e1b38010a4.
Thus the detailed SQL/cgroup/profiler writer is off, while normal source/error,
continuity/freshness, protective evidence and ledger checks remain enabled.
CPUQuota50% and MemoryMax128MiB unchanged. One financial writer; one existing
Telegram poller, neither analyst nor Telegram service was restarted.

## Safety and copies

Fresh read-only preflight found no active plan, open position, remaining entry,
reserve/margin/funding buffer or unfinished financial action. The server had
newer genuine history than yesterday:28events/68postings/10plans/6fills/1funding;
all of it was preserved. Free1000.032147110465389811USDT, ledger balanced.
Financial-table SHA256 before/after/restore:
4fe900de02e9ba5e941dca74e4fd18652a2405b2f6f784b0356b625c05aaba46;
plan-history SHA50ba7511aef7cd3ab6a60994a731b9e7b079c856fdaefd4c75f6909e53a318c7.
All IDs, payloads, balances and reserves were checked on an isolated completed
SQLite API copy, never a raw live-file copy. No opening capital, trade, fee,
funding or historical event was inserted/removed by this installation.

Fresh pre-switch local restore SHA
3fcbb139a25b5a101623f8b2b87a26d3aa23c1f03678f68a51e14c77dae96fcb,
reopened read-only/immutable, fully reconciled and matched all financial rows.
Pre-switch external upload/download/decrypt/restore manifest
c6c18565054236ce9c60b44c79c10f7c248ee5cf2af3632dd946d787e2e50121.
Flat/reconciliation/ingested-response/analyst-inactive were rechecked immediately
before paper stop. Existing analyst timer was briefly stopped for28.572s only
to prevent a new arm during switching, then restored ACTIVE/enabled with the
same next opportunity **17November16:02Baku /12:02UTC**; no skipped attempt
replayed. Analyst path74f77d0 preserved; API summary and latest saved response
identity equal before/after. Budget/journal/locks/credential untouched.

Fresh POST-install external restore completed **14:50:55.946Baku**:
manifest6c4ccb5545dada733a6d46b5d73e80e565fbc8009c045f7198eef2b6a6c8fbfd,
SQLitef418db0137d0530ecc197177229a25a82057487c5699d32444fc5c83b5e21260,
4chunks,28events/6fills/1funding, same balances and plan IDs. Existing mechanism
verified the downloaded manifest/chunks, decryption, full SQLite hash and
restored financial summary. New normal-profile startup local copy also reopened
and matched all financial rows, SHA
199919658de1aa79dfde65f90bb9cf8ecc9e606749b61f1e6a00d5071865e641.
Host-bound encryption still requires this host key; cross-host recovery is not
newly established by this rollout.

Rollback metadata, prior drop-in, preserved phase snapshot and local/external
restore receipts are under /var/lib/tradingbot-ai-budget-backup/release-b36103c.
Rollback means previous CODE only, only when flat; never an old ledger over
new events. Both releases and all current financial history remain available.

## Actual ordinary operation and /ai orders

An initial180s observation completed source/phase/financial checks; its verifier
then failed to import the existing UI file outside its Python package. This was
a verifier-only import error, not a live bot failure. Fixed the verifier to run
the ordinary renderer from the actual sole Telegram poller's deployment
/opt/tradingbot-ai-brackets-ui-c96ad52. No repeated installation or old suite.

Saved follow-up sample **14:50:06.799–14:50:36.166Baku**,60distinct publications,
max interval718ms,0market/funding/quote-rejection publications. The original
180s sample metrics were not persisted before that helper error and are not
invented. Exact main source BBO E/T and mark E were independently checked:
187/188ms BBO event/transaction age and871ms mark age at the sampled snapshot,
not receipt time substituted for source. No queue overflow/new reconnect.

One DATA_GAP after process start at14:44:57.332 is explicitly retained: bootstrap,
no active_armed_ms, history discontinuity between saved cursor and the newly
connected stream,24rows, oldest1106ms,0stale/future rows. It precedes first
confirmed-ready14:44:59.390, with no risk; not a later processing-latency gap.
No retrospective fill or invented bridge was made. No further DATA_GAP in the
queried durable messages through the saved observation; current errors null.
This finite empty-risk sample does not prove all historical latency eliminated
or live SL/TIME execution under an open position.

The unchanged Telegram renderer produced a valid fresh1534B /ai orders screen,
showing the actual free balance and existing states. No Telegram owner message
was fabricated and no model request/trade was used to demonstrate it. Final
post-backup ledger/hash/counts and publication matched; paper PID unchanged,
NRestarts0; analyst inactive/timer active. All four working wallets remain paused.

Reused published299unique targeted test IDs/417invocations and fixture proof
from the exact candidate. No full suite, backtest, training or demo rerun.
Additional AI calls0; synthetic/real/Testnet trades0; new automations/resources0.
Risk, leverage, budget and ordinary schedule unchanged. No diagnostic follow-up
was created. Installation blockers: **none** at the final check. Remaining
limitations are host-bound external restore, no new live-risk execution proof,
and no assertion of strategy profitability or guaranteed source availability.
