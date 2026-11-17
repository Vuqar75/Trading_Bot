# Paper execution release — 17 November 2026

This candidate continues `e3b4878`, preserving the installed WebSocket depth/mark
sources, diverse LONG/SHORT candidates, immutable financial history and single
financial writer. It is a test/paper release candidate. It does not establish
profitable decisions, real exchange fills, or a bound on source/network outages.
**It has not been installed on the VM by this cloud session.**

## What is delivered

Normal `serve` no longer starts the CPU/SQL profiler, instrument every SQLite
call, read scheduler/cgroup counters around every verification/commit, or create
a detailed diagnostic writer. Those development tools are opt-in with
`--phase-diagnostics`. The existing source timestamps, independent component
freshness, gap/reconnect/overflow gates, market errors, cancellation reasons,
protection-trigger/wait/fill evidence and financial verification remain enabled.
Existing private diagnostic files are preserved. Market-data failure still
blocks unsafe entry; an exit without confirmed fresh prices remains pending.

A deterministic measurement on this cloud CPU with 100 identical fixture cycles:
normal profile 0.0686s process CPU /0.0688s wall; detailed profile 0.1510s CPU
/0.1495s wall. Both reconciled. This demonstrates optional instrumentation cost
in that experiment; it does NOT identify the cause of each historical VM stall,
predict a VM speedup, or prove all DATA_GAP eliminated.

The existing `research.ai_limit_demo` now drives the **production**
StreamFeed → quote/trade parsing → Poller → grouped Book transaction path.
It does not bypass that path with direct fabricated Book.market fills.
The model transport and public events are explicitly fixtures; there are no
sockets/model requests. It exercises:

* Closed causal candles → normal candidate policy → strict model request.
* Generated test-only budget reservation, saved response, usage, binding and ingest.
* Fresh depth5+mark components, stream bootstrap/continuity and partial entries.
* LONG → TP and SHORT → SL after process restart.
* SHORT → TIME while both quote channels are down: retains open position and
  EXIT_PENDING, restores the ledger, then exits only on fresh recovered prices.
* Duplicate trade idempotency, WAIT with no new order, funding finalization,
  SQLite backup restore, and the ordinary read-only Telegram renderer.

Run on Linux/WSL in a new temporary directory:

```bash
python3 -B -m research.ai_limit_demo --output "$(mktemp -d /tmp/tradingbot-paper-proof-XXXXXX)"
```

`result.json` identifies `fixture_only=true`, `real_model_calls=0`, production
execution path, three CLOSED plans (TP/SL/TIME), WAIT, nine fills, zero remaining
reserves/margin/buffer and balanced ledger. The generated balance/PnL is a
**synthetic mechanism outcome, not a strategy result**. No real budget or VM
ledger is opened/copied. The fake original reservation and lock files are
created only under that output directory. Live runtime rejects fixture answers.
A saved example and rendered screen are in `research/ai_limit_bracket_results/`.

## Verification already performed

299 unique targeted test IDs passed in 417 invocations (118 additional
invocations arise from existing fixture TestCases imported by other modules;
417 must not be presented as 417 distinct tests). The suites cover runtime,
financial bracket rules, current WebSocket source, protection evidence, loop,
backup/journal recovery, cancellation, executor and optional diagnostics.
The final new test runs the actual `serve --once` entrypoint with a controlled
stream, no network/model and no profiler: public snapshot is balanced/valid,
initial activation respects the normal code path and no diagnostic file starts.
Both profiles produce identical tested SL close, fees and balances.

The end-to-end module also ran as a separate CLI process. A preinstalled client
dependency was absent in this cloud workspace; installed the repository's pinned
`websocket-client==1.8.0` before validating its native invalid-UTF8 test. No VM,
credentials, personal Telegram config, paid model request, real/Testnet order,
working-market pause, risk, live budget, schedule or resource limit was changed.

## One final installation by the already-authorized local operator

1. Fetch the candidate and read current HANDOFF/AGENTS. Check actual live versions
   and preserve newer work. Verify manifest hashes. **Only ai_limit_runtime.py
   needs changing in the active paper release**; the updated demo is an optional
   standalone fixture tool. Do not replace analyst/UI, financial DB, candidate
   rules or entire deployed application. No new package dependency was added.
2. Inspect fresh live risk; never assume the earlier flat report is current.
   Coordinate the existing analyst timer only for the brief switch to prevent
   arming a plan during maintenance, preserving its original state/schedule.
   Do not replay a skipped opportunity or create an extra analysis request.
   Never stop/restart protective paper execution with outstanding risk.
3. Create a consistent ledger backup and verify isolated restore, IDs, balances
   and reserves. Recheck flat state immediately before switching. Use the normal
   retained-release/code rollback procedure. Do not restore an old ledger over
   newer events. Keep all CPU/resource settings unchanged for this deployment.
4. Start ordinary `serve` without `--phase-diagnostics`, preserving its other
   arguments. Check the existing unit has no diagnostic-only wrapper/flag that
   forces tracing. Confirm one writer, expected process/publication, fresh source
   components, intact history/ledger and `/ai orders`. Restore the existing
   analyst timer state and verify the next scheduled opportunity; do not send a
   paid request or forced trade to prove installation.
5. Verify fresh external ledger backup restore. Publish installed hashes, actual
   risk, ordinary source status and any concrete remaining blocker. This is a
   finite release handover, not a new chain of diagnostic automations. Per the
   owner's latest STOP, do not create recurring or one-shot diagnostic tasks.

**Completion boundaries:** the cloud delivers a code candidate and repeatable
paper-path proof. Only the authorized local operator can confirm deployment and
fresh live risk on this VM. A green fixture run is not live under-risk evidence,
and a working paper bot is not proof of profitability. Its normal permitted
schedule may choose WAIT or leave a passive entry unfilled. Those outcomes alone
are not defects. A proven failure of execution, protection or accounting is.
