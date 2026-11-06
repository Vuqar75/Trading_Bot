# Adaptive research study: installation and recovery plan

This is a plan, not proof of installation. Follow the pre-start
`ADAPTIVE_CONTINUOUS_PROTOCOL.md`. Never attach the study to trading wallets,
the Telegram queue, the independent observer or the AI budget database.

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
