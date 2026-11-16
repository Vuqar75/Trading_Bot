# 23:45 slow-span follow-up — no new gap, no speculative update

Performed16November2026 at23:45:57.749Asia/Baku, additional read-only health
23:48:05–07.332. Fetched research/ai-limit-brackets, clean initial HEAD4f1829b,
no newer remote changes. Read AGENTS/HANDOFF and the23:00 report; did not repeat
the old six gaps or retrofit new counters onto the23:00:10.698 event.
Exact470904B raw preserved on D:/Documents/Codex/stream-work/phase2345-raw.json,
SHA256 a0d658b459c0866b3706105e02ac23e42e70db4f180085965dc8310074c8ec24.

[Evidence](research/AI_BACKLOG_2345_EVIDENCE_20261116.json). Exact private JSON
copied to D:/Documents/Codex/stream-work/phase2345-raw.json BEFORE any change,
470904B, SHA256 a0d658b459c0866b3706105e02ac23e42e70db4f180085965dc8310074c8ec24.
No runtime or VM configuration change was made.

## Result and attribution limits

**Zero new DATA_GAP in durable bracket_messages after verified ready23:16:07.594
through the23:45:57 collection** (about29m50s). Probe v2 has only the preserved
startup/bootstrap episode23:16:05.340,14cycles,post_remaining0, not a new live
failure. No evicted episodes or coalesced publications; writer statusOK.
Private capture23:45:55.976, public23:45:55.487. SamePID521280/NRestarts0.

Latest64cycles cover38.338s monotonic; max main cycle2500.376ms. New slow-phase
fields are present, with actual main nativeTID and per-span counter boundaries.
The table is MAXIMA PER NAME among retained >100ms spans, not one common cycle
and not totals; nested phases/parallel workers must not be added.

| phase | retained slow spans | max wall ms | calling-thread CPU ms | main ready-queue wait ms |
|---|---:|---:|---:|---:|
| main_poller |22|2256.450|251.483|1984.364|
| book_verify |15|1525.473|228.816|1090.730|
| main_snapshot |18|1737.669|228.451|1298.719|
| main_publish |5|241.850|2.062|233.063|
| sql_commit |2|1222.582|220.941|238.788|

These ordinary spans establish that runnable waiting remains material, including
publication with only2ms CPU but233ms ready wait. They do NOT identify which
unrelated service caused the delay or prove the23:00 gap mechanism. Cgroup
throttle is all-thread and not directly assignable to the main thread.

Important metrology caveat: the longest commit reports220.941ms calling-thread
CPU but only1.256ms schedstat-runtime delta. Resource counters are read
sequentially, and wrapper CPU includes before-sampling work; their endpoints
are not an atomic common snapshot. Do not force equality with a tolerance,
subtract mismatched clocks to invent I/O time, or label that220ms as SQLite
calculation. Exact individual counter-read latency was not captured in that
historical span. A NEW tiny, nice15 read-only check (3 reads each of rusage,
schedstat,cpu.stat, no artificial load) observed max214.950us; it did NOT
reproduce a slow read and is not a worst-case guarantee under service contention.
No cause or correction is invented from this inconsistency.

Four unique diagnostic writer results in the saved tail. Latest separates
24.644ms serialization (9.112ms thread CPU) from1236.848ms disk wall,
1261.487ms total. The writer is asynchronous/queue1; this does not establish a
same-duration main block or GIL cause. No new gap occurred to pair with these
fields. CPU quota, background filters, ordinary VM work and I/O cannot be
ranked as the cause of a NEW failure that did not happen.

No reproduced program bottleneck, no speculative patch/quota/ranking/freshness
change, no regression suite or installation. Existing diagnostics remain
bounded and active for an actual future episode, without another identical
scheduled chain. This check is complete; underlying historical latency is NOT
claimed eliminated and this empty-risk interval is not open-position protection
validation.

## Safety, sources and existing backups

Actual installed runtime remains **e1e61326471ce1ec89f8688446bb3461b4ed0104**,
/opt/tradingbot-ai-brackets-e1e6132-paper, probe SHA256
208267a88330a3b313313ec2d4be7e02c2af606a2921fe4266735b59f018bb04.
Analyst74f77d0 inactive; authorized timer active, next17November00:02Baku
(16November20:02UTC), unchanged and not invoked. One financial writer and one
Telegram poller. Current trade queue0, highwater292/32768, duplicates0, no fault;
connection counter1 unchanged from initial connection, no new reconnect.
Latest BBO/mark channels connected; current time_rejections empty. At final
publication23:48:07.332: BBO Eage1224ms,Tage1226ms, markEage1332ms; source E/T
are not replaced by receipt time. This is snapshot freshness, not proof that no
component ever had a transient refusal throughout the whole interval.

No active plan/position/remainder/reserve/margin/funding buffer; free
1000.309096007865389811USDT. Ledger balanced;24events/9plans/4oldfills/1funding,
history SHA3507d3cbeb12cde1c3c4207ba5229b65712d736c79863151bcbd7d4a20d3e8c6
unchanged. Four working wallets remain paused. No additional AI request, model
retry, created trade, capital/budget/credential/lock/risk/schedule change.

Existing local snapshot1794856562431.sqlite reopened READ-ONLY: integrityOK,
24events, balanced; SHA5a5bb9cdeef7d5bede10d9587bb2634e1fbd487ef4b1f85a6fa5b35e3afd0178.
Last existing external receipt **23:29:06.026Baku**:
manifest f560da050caa819de3df671b6a4b105d9db78015da2221547daf6b58d1f3d45d,
SQLite cc8ca12b03f961bb4de1795cbe69bfd21ec8e93fffc6f1a15ce7505db2fbc918,
4chunks/24events/4fills/1funding. Existing backup code writes this receipt only
after download/decrypt/restore/hash and summary equality; no backup rerun or
new download was initiated in this check. UnitResult=success/ExecMainStatus0;
main exit23:27:17 precedes the later receipt, not treated as its timestamp.
Host-bound cross-VM recovery limitation remains.

**One-shot trading-bot-attribution-of-slow-main-spans deleted with tool
confirmation before23:53:03.929Baku.** No new identical one-shot registered:
there is no new event to reproduce and no new distinct experiment justified.
The authorized analyst/service schedule and protection keep running unchanged.
