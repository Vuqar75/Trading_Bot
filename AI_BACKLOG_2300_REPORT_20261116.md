# 23:00 backlog: measured scheduler delay; missing attribution installed

Read-only capture 16 November 2026 23:01:21.193 Asia/Baku; continued de6d74d.
One NEW post-install gap at **23:00:10.698 Baku**, separate from the six earlier
18:33–19:05 events. [Evidence](research/AI_BACKLOG_2300_EVIDENCE_20261116.json).
Raw757646B remains on D:/Documents/Codex/stream-work/phase2300-raw.json;
SHA256 ad1d41d183e54c1535088ae25d88c63337b052f7299b753cf6188a59cbc55158.
All original versions retained; no retrospective prices or invented counters.

## What is established

160 WS rows, IDs3528342459–3528342618, no ID discontinuity/future event/socket
fault/queue overflow.25 rows stale; oldest source23:00:04.750 was5944ms old
at dequeue23:00:10.694, oldest measured in-process queue wait4261ms. Newest
source age220ms. This is local consumer backlog, not evidence of1000-row REST
saturation or proof of an exchange delivery outage.

The preceding main loop23:00:04.932–10.433 consumed5500.3ms wall /701.8ms
calling-thread CPU. Counter interval5501.048ms, main schedstat runtime702.612ms,
ready-queue wait3547.125ms. Resource deltas:55 quota periods,38 throttled,
4455.067ms cgroup-wide throttle,1431.726ms all-thread CPU. Cgroup throttle is
not main-thread delay and cannot be subtracted/summed with nested phase walls.
Main read/write blocks +72/+128, no new major fault; actual process read36864B,
write65536B. Host CPU PSI high. Ready-queue delay is directly measured; exact
share attributable to quota versus other runnable VM work is not established.

| preceding main phase | wall ms | calling-thread CPU ms |
|---|---:|---:|
| poller including first snapshot |4504.936|468.456|
| final snapshot |747.281|231.474|
| publication |10.000|1.625|
| sleep |237.788|0.024|
| SQL commits, nested |235.968|1.801|
| two snapshots, nested |4738.009|683.689|

Book verification and commit are not proven expensive calculations or slow disk
merely because their wall spans are large. The background exchangeInfo request
was5996ms plus1510ms parse (7507ms total); this was NOT a synchronous protective
REST quote call. Background CPU/GIL interference remains possible, unproven.
The diagnostic writer previously took641.6ms/123.3ms CPU; v1 did not split JSON
encoding from disk wait. WS recv wall includes quiet socket wait, not delivery
latency. Do not add simultaneous thread/nested phase durations.

Local background backup had completed at22:20:24.953, long before this gap.
No external-backup service in the narrow23:00 interval. Adaptive regular service
finished23:00:08.953 after5.221s CPU; watchdog earlier22:59:41.951–46.929.
Overlap is association. We did not stop or retune these services.

A restored-copy profile found no stable single heavy SQL/calculation: three
uninstrumented snapshots24ms once, instrumented496ms once, with wall samples
landing on unrelated tiny functions. Those short non-concurrent observations
are NOT a causal before/after performance comparison or justification to change
financial verification. No speculative ranking/risk/CPU quota adjustment.

## Minimum missing measurements delivered

Only research/ai_limit_phase_probe.py changed at runtime. v2 adds scheduler/
quota/switch counters at boundaries of slow main poller, verify, snapshot,
commit and publish spans (>100ms diagnostic retention threshold, not freshness).
Maximum24 spans per cycle; original64pre/8post,8episodes/2MB, queue1 writer and
private0600 file retained. Main identity checked; no SQL parameters/payloads.
Writer now separately records serialization wall/thread CPU and disk wall.
These distinguish local descheduling within phases from diagnostic encoding and
write wait. They do not prove which unrelated task caused host contention.

Temporary tests atCPU50%/128MiB:55 of57 passed initially; only two missing vendor
websocket imports failed. With the existing installed vendor path, just those
two passed; no unchanged suite rerun. Four new tests cover artificial slow
spans, reset/bounds, main-vs-background identity, unavailable counters, and
split encoding/disk wait. Existing instrumented partial/SL/TIME/replay checks
remain covered. Sampler20-call median76.450us,max510.360us on temporary copy;
not a bound under contention. No artificial live load or trade.

Installed **e1e61326471ce1ec89f8688446bb3461b4ed0104**, ready23:16:07.594Baku,
PID521280, /opt/tradingbot-ai-brackets-e1e6132-paper. Exactly one module hash
208267a88330a3b313313ec2d4be7e02c2af606a2921fe4266735b59f018bb04.
Fresh pre-switch external upload/download/restore receipt manifest
1c428805ef15c655a4549c8aa9782e6205e6ab6ce185a3a936e4935649b9578b.
Flat-only guarded switch; analyst74f77d0, CPU50%/128MiB, funding/risk/5s/budget/
pauses/schedule preserved. Old phase file and code-only rollback drop-in saved
under /var/lib/tradingbot-ai-budget-backup/slow-span-e1e6132; keep WAL/new events.

Financial history hash3507d3cbeb12cde1c3c4207ba5229b65712d736c79863151bcbd7d4a20d3e8c6,
24events/9plans/4historical fills/1funding unchanged, free1000.309096007865389811,
no active position/order/reserve/margin/buffer, balanced. Last scheduled AI20:02
WAIT unchanged; no model call or artificial trade. This flat observation cannot
verify real-position protective latency. Underlying scheduling problem is NOT
claimed eliminated. New future data is needed for the specifically missing
within-phase attribution, not another identical empty-account check.


## Concrete continuation registration

The completed trading-bot-cpu-sqlite-backlog one-shot was deleted with tool
confirmation. A DIFFERENT, specific missing-data check is actually registered:
**trading-bot-attribution-of-slow-main-spans**, ACTIVE, created
16November2026 **23:19:25.137Baku**, single launch **23:45Baku /19:45UTC**.
App view and stored registration verified. The first create was rejected for
missing target-thread identity; only the successful explicit thread registration
is counted. It reads new per-span scheduler/encode/disk evidence after the
existing hourly backup opportunities, not another empty-account cycle. It must
reproduce/fix only established bottlenecks, preserve safety, save evidence and
delete itself; no identical chain absent a new testable question.


## Actual post-installation observation and copies

180.387s ordinary flow,254 distinct publications, max publication interval3102ms,
zero error publications. Only startup/bootstrap gap23:16:05.340, before verified
ready23:16:07.594; no subsequent DATA_GAP or quote rejection in this window.
41 slow spans in the saved64-cycle tail demonstrate the NEW fields are live:
main_poller2222.237ms /443.357ms CPU /1532.951ms ready wait; main_snapshot854.132ms
/206.892ms CPU /642.966ms ready wait. Nested book_verify is the same interval,
not additional delay. Normal writer separated5.150ms serialization /13.326ms disk.
These are ordinary spans, not retrospective reconstruction of the23:00 gap.

Local startup backup completed2832.803ms,195steps/0restarts,6233pages,24events;
archive reopened integrityOK and ledger balanced. Main only submits, no inline
wait. Fresh POST-install external upload/download/restore verified
**23:21:56.385Baku**, manifest
70ac1c99b63b8f0cf4ff16867a6cb1f1e4e80895e582694f9fda590465a01b8d,
SQLite960a36ba60c703583256ff6a174feb7b22de3ce0aaf0241e3dd67056aa6fdc3f,
4chunks/24events/4fills/1funding. Existing host-bound encryption caveat retained.
Post-backup read-only health **23:22:56.156Baku**: samePID521280/NRestarts0,
current publication23:22:54.669, no new post-ready gap, no quote time rejection,
market_errors null, no active plan/risk/reserve, ledger/hash/counts unchanged,
one writer/one Telegram poller, four working wallets paused. New writer sample
separates168.736ms encoding (137.163ms CPU) from225.458ms disk; no assumption
that all of that time blocked the financial main thread.

V2 raw first capture444309B SHA68937a8c3c1906df87f65c583683b13e2786a946cd51fb0da6f835451d49ecd9
on D:/Documents/Codex/stream-work/phase-e1-raw.json; later raw/hash and complete
restoration receipt in evidence. No model invocation, execution, new paid resource,
capital/budget/pause/5s change. No full application suite/backtest/training.
Underlying shared scheduling/possible GIL pressure is still unresolved; v2
counters are installed and verified, NOT a claimed latency cure or live-risk proof.
The23:45 registration specifically captures a NEW gap with these counters.
