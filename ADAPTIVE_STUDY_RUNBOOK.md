# Adaptive research study: installation and recovery plan

Follow the pre-start
`ADAPTIVE_CONTINUOUS_PROTOCOL.md`. Never attach the study to trading wallets,
the Telegram queue, the independent observer or the AI budget database.

## Installed study, 2026-11-06

The installed study code tree is GitHub commit `b95c1ca18ce2b21da17d85f709bf7f5535ac81b5`
(local equivalent `0cf2277d5d09bd1b05df08d4680f66e642ae23af`, identical tree).
The sole `adaptive-study.service` began at 2026-11-06 22:48:12.589 UTC
(2026-11-07 02:48:12.589 Asia/Baku). Both separate experiments began in cash,
1,000 USDT each, with no positions. Historical quotes do not authorize entries.
The first eligible 4-hour decision boundary is 2026-11-07 00:00 UTC; the
existing AI service has its own permitted collection window and may legitimately
miss it. A future same-snapshot AI/baseline result is not yet proven.

The initial 23:00 UTC candle window was missed due to a scheduler boundary
race; it is explicitly recorded as a gap and was not backfilled. The installed
revision corrects the race and detects missed boundaries. At 23:15 UTC the
installed service accepted both the Spot and USD-M closed candles, with no
new gap or source error. An independent
systemd test and a subsequent real budget publication also confirmed that
watching the source directory detects atomic snapshot replacement.

The local status is `/var/lib/adaptive-study/state/status.json`, readable with
`sudo -u adaptivestudy cat /var/lib/adaptive-study/state/status.json`. It is
not currently connected to Telegram. `adaptive-study-publisher.path` passes
sanitized evidence through a root-owned directory, not the AI credential or
budget. The executor's evidence opt-in was installed outside its request
window; the existing timer and budget were retained. An old response cannot be
retrospectively bound to candles that were not recorded at the time.

`adaptive-study-backup.timer` and `adaptive-study-upload.timer` schedule the
separate snapshot and readback-verified external upload. The initial actual
study archive was restored in isolation and verified, SHA-256
`cfe611800de10e320abffe30839adb1973ce1e5a84af35832fca698668199a9b`.
After the first accepted production candle, a newer external copy was
read back and restored, SHA-256
`88c4a6982123c8d086be7e1903d7e004e9a2cea73ab6324ea70a9b4632c92e6e`.
The service account used by the uploader has existing bucket-wide authority;
the research prefix is an application boundary, not an IAM restriction.
Bucket lifecycle and off-host recovery of unrelated host-bound encrypted AI
budget backups have not been established here. Backup size caps can require
operator attention as the research history grows.

Public Spot and USD-M data and funding are research inputs, not a guarantee of
executable prices. The system assumes complete fills and cannot partially
close isolated positions; a daily or periodic protective check is not an
exchange-held stop. Missing or contradictory funding remains unknown, blocks
entries and may prevent a financially complete close; price-based protection
is still evaluated where available. The study strategies previously failed
economic selection and remain research-only.

## Prepare and rehearse

1. Check the actual VM's service versions, systemd units, PIDs, workload,
   memory and free disk. The snapshot and uploader are scheduled away from
   the existing 03:15 and 03:30 observer backups; actual load takes priority.
   No trading or Donchian service is restarted. Verify that cgroup IP filtering
   blocks metadata access while ordinary public Binance access remains usable.
2. Before starting the study, commit the exact protocol and code, run the
   targeted Linux bench and validate the installed unit copies with
   `systemd-analyze verify`. Test the service sandbox, single-writer lock,
   isolated two-account startup, data outage and restore on temporary paths.
   Preserve all rehearsal evidence. Check actual unit dependencies; do not
   assume a previous VM version from this document.
3. Stage immutable root-owned code under `/opt/adaptive-study-<commit>`, verify
   its exact Git tree, and use root-owned `/opt/adaptive-study-current` for the
   units. Create only the two dedicated system users/groups required by the
   templates; fail if an existing name has conflicting ownership. Create
   private state owned by `adaptivestudy`, and a distinct upload staging area
   owned by `adaptivestudy:adaptivebackup`, setgid 2750. Root owns the sanitized
   publisher output directory, group `adaptivestudy`, mode 0750. The uploader
   status directory is owned by `adaptivebackup:adaptivestudy`, mode 0750.
   The uploader must not be able to read study databases directly; the study
   user must not read the AI budget, executor-private files or credentials.
4. The existing `adaptive-ai-regular` executor needs an opt-in code change to
   capture its own actual raw inputs before reservation. Outside any request
   window, first verify the service is idle, record its unit/drop-ins, timer
   state and current code, budget/lock checksums, and take a verified existing
   budget backup. Rehearse a unit-only switch and rollback on copies. Add a
   root-owned drop-in selecting the exact verified candidate and
   `ADAPTIVE_EVIDENCE_DIR=/var/lib/tradingbot-ai-study-export`, with an
   additional `ReadWritePaths` for that `adaptiveai`-owned 0700 directory.
   Preserve its existing encrypted credential, budget path, authorization,
   limits and timer schedule. Never invoke the model to test the switch. If
   anything fails, restore the saved drop-in and timer state; never overwrite
   the journal or replay an uncertain request.

## Start and inspect

Enable the evidence publisher path and the one study service only after the
above rehearsal. Its start file is created exactly once; a partial initialization
requires review rather than re-seeding. The first eligible 4h decision boundary
must be later than this start. Both accounts start with cash; no working funds
are moved. No artificial trade is required.

```
sudo systemctl status adaptive-study.service adaptive-study-publisher.path
sudo -u adaptivestudy cat /var/lib/adaptive-study/state/status.json
sudo systemctl start adaptive-study-snapshot.service
sudo systemctl start adaptive-study-upload.service
sudo systemctl status adaptive-study-backup.timer adaptive-study-upload.timer
```

These are inspection/installation commands for the authorized operator, not
evidence that they have already run. Confirm the real public quote event times,
closed candle watermarks, admitted post-start decisions, source pairing, both
account balances and protection status. Verify `IPAddressDeny` is effective
on this VM, rather than trusting a unit declaration. Confirm absence of extra
pollers, no access to the metadata token or private trading directories, and
unchanged trading and observer PIDs/pauses. Verify the uploaded object by
readback and isolated restoration; inspect the returned hash and backup status.
The uploader has the VM's existing bucket-wide service-account permission;
prefix scoping here is application policy, not an IAM restriction. Bucket
lifecycle/retention remains to be independently confirmed.

## Restore or stop

Stop only `adaptive-study.service` before recovery; the publisher and trading
services need not stop. Preserve the newest state and verify no study writer
exists. Download a chosen archive into a temporary directory, run the archive
verifier and compare both financial journals, queue, route, observations, market
history, start and hashes before deciding whether restoration is safe. Never
replace newer events with an old snapshot. Do not start a restored instance in
parallel with the current writer. If a funding/candle contradiction or
ambiguous action exists, retain it and show blocked/unavailable health; do not
auto-repair history or backdate trades. Restarting the host has not been tested
by an actual reboot; validate unit enablement and isolated restart behavior.
