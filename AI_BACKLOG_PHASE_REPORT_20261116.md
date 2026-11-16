# Backlog phases and local backup repair —16November2026

**Installed paper d3eb8841895155059ac7aaed7e18f8addb0c5e48 at 2026-11-16T22:20:23.145+04:00**, `/opt/tradingbot-ai-brackets-d3eb884-paper`, PID518561. Analyst74f77d0 unchanged. [Evidence](research/AI_BACKLOG_PHASE_EVIDENCE_20261116.json), [protocol](AI_BACKLOG_PHASE_PROTOCOL_20261116.md). Latest saved verification 2026-11-16T22:30:22.432+04:00. No strategy, working-market or paid-model experiment.

## What the six old records prove

All six18:33–19:05 records have contiguous observed IDs, no socket reconnect/fault/overflow, but3.604–7.541s already spent in the local dequeue queue. The917ms interval at18:33 coexisted with5195ms queue wait and3460 queued events: accumulation spans multiple cycles. No source-time ID density is treated as network delivery rate or exact lost-event count.

|Baku|Oldest trade age ms|Dequeue wait ms|Poll interval ms|Queue remaining|Observed overlapping services|
|---|---:|---:|---:|---:|---|
|18:33:37.145|5870|5195|917|3460|adaptive-study-telegram.service, adaptive-ai-regular.service|
|18:35:32.155|10374|7541|8537|1812|adaptive-study-telegram.service, adaptive-ai-regular.service|
|18:37:25.268|5113|3604|557|2154|tradingbot-watchdog.service, adaptive-ai-regular.service|
|18:45:36.741|5146|4495|3747|278|adaptive-study-telegram.service, adaptive-ai-regular.service|
|19:00:45.315|6507|4991|2502|623|adaptive-ai-regular.service, tradingbot-external-backup@hourly.service|
|19:05:57.943|5039|3624|4871|49|adaptive-ai-regular.service, adaptive-study-telegram.service|

Systemd history was read for the saved event windows. adaptive-ai-regular overlaps6/6, Telegram publication4/6, watchdog one window; the19:00 event also overlaps external hourly backup. These are associations, not causal attribution. Internal paper backup duration is unknown for five episodes; the last retained loop has backup_ms0. Historical per-episode CPU/IO/cgroup deltas were not recorded. A single last_gap_phase_diagnostics metadata key overwrote detailed context of earlier episodes. Old cumulative throttling cannot reconstruct those missing deltas. Only the last loop retains snapshot2314ms/poller2151ms/total4872ms. Therefore exact common CPU-versus-SQLite/commit source for ALL SIX remains unknown, not an exchange fault by assertion.

## Confirmed new defect and its completed fix

New installed diagnostics captured an ordinary startup local backup **inside the main financial loop**. Legacy backup stage14527ms; measured copy/verify wrapper 14.496839s wall versus 2.866416s thread CPU. The main writer could not inspect protection during that call. Account was flat, so no live position delay/PnL consequence is claimed. This is a newly proven independent defect, not retroactive proof about all six old episodes.

Reproduction and correction proceeded in existing code:

1. Slow temporary backup reproduced the synchronous wait. A single bounded background BackupJob uses a NEW READ-ONLY source connection and the existing SQLite backup API; it never executes financial events. Main loop submits, never waits for completion. Partial SL/TIME detection/exit and no-double-posting fixtures run while the backup is deliberately held by an Event.
2. First incremental rollback-mode live copy safely failed at20.026s rather than blocking protection. This failure is retained, not reported as success. A temporary8MB fixture with repeated metadata writes reproduced8 copy restarts/no completion in rollback mode; a pinned WAL read snapshot completed with0 restarts while9 writes continued. These are technical fixtures, not trade simulations or measured market delivery rates.
3. Final source is **WAL, synchronous=FULL**, only for this isolated paper DB and only at a flat startup. Existing order/risk in a legacy DB defers conversion and keeps protection running. One main financial writer also owns automatic checkpointing; backup sources are readonly,32page steps,20s deadline checked between steps. No separate live checkpoint/writer, budget, capital, risk or5s freshness change. Working shared DB remains DELETE. Do not plain-copy/delete a live .sqlite/WAL/SHM set.

[SQLite backup documentation](https://www.sqlite.org/backup.html) describes restart on other-connection source changes and possible noncompletion. [WAL documentation](https://www.sqlite.org/wal.html) explains stable read snapshots concurrent with writes. Installed engine3.45.1 (Ubuntu package3.45.1-1ubuntu2.8) was not upgraded. The upstream WAL-reset race warning was reviewed: no claim that this version string contains its fix; the implementation maintains exactly one writable/checkpointing connection per LIVE DB, all backup sources readonly. Do not add a second live writer/checkpointer without a version review. Existing immutable external restore inspector was tested on the WAL-produced backup.

## Bounded diagnostic implementation

Inside the existing process: per-phase monotonic wall and calling-thread CPU for WebSocket receive/parse/dispatch, dequeue/parse, trade batch, SQL execute/fetch/iteration/commit, verify/snapshot, public publish, sleep and backup submit. No SQL text/parameters, response bodies or credentials recorded. Receive wall includes waiting for a frame, not a source timestamp or proven network latency. Event E/T and source/receipt remain separate.

Each new gap freezes64 preceding and8 following cycles, up to8 episodes/2MB JSON, with eviction/coalescing counters. Resource snapshots cache at most1s and carry sample_ns: cgroup CPU, main scheduler CPU/ready-wait, context switches/block I/O, process I/O, host steal/iowait ticks and CPU/IO PSI. Durations are inclusive/nested; clock overhead matters at microsecond scale. Snapshot/SQL/phase times must not be added as independent exclusive work. Two diagnostic files can temporarily occupy up to4MB with the atomic temporary file. One background evidence writer, queue1, coalesces updates and never waits in the financial path; normal publication every10s and on gaps. CPU/file-writer costs remain visible. File `/var/lib/ai-bracket-study/phase-diagnostics.json`, private0600; no new service or collector. Code switches archive the preceding diagnostic file in the release metadata; no financial history reset.

## Tests and actual observations

-57 focused instrumentation/protection/loop tests passed, including uninstrumented and instrumented compatibility, slow commit, rollback, slow diagnostic disk and bounded history.
-15 final phase/dequeue/protection checks passed after adding pinned batch fields.
-8 backup tests passed on final causal assertions: slow copy does not gate SL/TIME, no overlap, timeout cleanup, existing archive preserved, partial-position restoration. Initial arbitrary0.5s fixture assertions failed0.531/0.709s under CPU quota; replaced with direct synchronization proof that protection completes before the held backup is released. This is not a relaxed5s market limit or a real-time SLA claim. Initial fixture/AST harness failures are retained in D:/Documents/Codex/stream-work and temp logs.
-2 rollback/WAL reproduction tests,32 WAL protection/backup/legacy-risk checks, and2 final FULL/immutable-restore checks passed. Same CPU50%/MemoryMax128MiB. No full application suite, strategy backtests/training or extra AI request; common trading_bot financial code unchanged.

Final live publication window 2026-11-16T22:21:13.019+04:00–2026-11-16T22:24:13.070+04:00:269 distinct saved samples over180.051s; collector193.405s includes waiting for external backup. No new DATA_GAP after separately labeled startup bootstrap, no quote error/reconnect/overflow. Max observed main interval4250ms; this does not guarantee future latency or active-risk execution. Current ledger was flat. Nonblocking backup completed in **5.242117s**,24events,25522176B,195 steps,0 restarts. Reopened integrity/reconciliation pass. Kernel I/O/CPU scheduling may still delay work; the20s check is not an OS syscall cancellation guarantee.

New measurements also demonstrate remaining scheduling pressure, without claiming an old-period cause: the intermediate ordinary ff858aa sample had18.062s main ready-queue wait over39.506s resource interval, cgroup throttling delta22.798s across its threads and high host CPU PSI. Final saved recent-window ready-wait delta6.216s/20.418s. SQL commit wall/CPU and public write waits are now separate. Quota/VM sizing/schedules were NOT changed speculatively. The new records are what the23:00 check will use if another episode appears.

## State, restore and continuation

Financial history hash `3507d3cbeb12cde1c3c4207ba5229b65712d736c79863151bcbd7d4a20d3e8c6` unchanged:24events/9plans/4old fills/1funding, reconciliation balanced; free1000.309096007865389811USDT, all exposure/remainder/reserve/margin/buffer0.1financial writer/1Telegram poller; existing /ai orders render valid. Four working wallets paused, their shared journal still DELETE. Same CPUQuota50%/128MiB, VM kept running, analyst schedule unchanged (next00:02Baku), old studies/Donchian not enabled. No additional model call, artificial/real/Testnet trade or paid resource.

Pre-switch external upload/download/isolated restore manifest `960f0ea240390e9a8a9e4c80300bfe523d24a56a3b62fd2d4add89836f7c80e2` was checked before flat recheck/stop; only paper unit switched. Latest post-WAL external restore receipt **2026-11-16T22:28:48.710+04:00**,4chunks/24events/4fills: manifest `0020b27fda4e87468c3c0495079ca750e738bc96740dd32b36cfe2c29602426f`, SQLite `accbd6a83b5f4b4a7580f41cbd9a8149034cab36a876ed6a45c9c0e85006fcf6`. Host-bound encryption limitation unchanged. Code-only rollback retained in `/var/lib/tradingbot-ai-budget-backup/backlog-d3eb884/previous-dropins.json`; preserve new events/WAL, never restore an old DB over them. Old Book code can read WAL, but rollback would reinstate old behavior, not justify deleting sidecars. Restore uses SQLite API, not ordinary copying of live files.

Evidence directories preserve all candidate measurements, including failed intermediate copy: `/var/lib/tradingbot-ai-budget-backup/phase-probe-8641b59`, `phase-probe-0c82200`, `backlog-ff858aa`, `backlog-d3eb884`. Raw diagnostic capture on D:/Documents/Codex/stream-work/backlog-phase-d3eb884.json, SHA256 `a9c02dd59a526b527e39bf3f35d3ed6de6db4e17052412e50b89bbd2e342b4c2`,380216B; raw data not added to Git. DB25522176B, VM free12063301632B.

Exactly one concrete follow-up **trading-bot-cpu-sqlite-backlog** actually registered **16November21:58:47Baku**, confirmed ACTIVE for **16November2026 23:00Asia/Baku /19:00UTC**. Prompt updated to installed d3eb884; app update/restart did not require repeating completed tests/install, registration re-opened afterwards. Its goal is missing per-episode CPU/SQL/queue attribution and background-backup evidence, not another empty-account AI check. Save raw data/hash first, do not infer six historical causes from new counters; reproduce/fix only established defects and delete the one-shot afterwards.

Final read-only health22:41:14.352Baku: same PID518561, current publication22:41:13.843 and probe22:41:11.586, writer OK, no new episode beyond startup, no active plans/reserves/market error, ledger balanced, local backup complete/0restarts. No repeated installation or test suite after the app update.
