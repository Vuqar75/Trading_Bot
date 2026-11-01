# Актуальное состояние после установки 1 ноября 2026

Единый paper-журнал установлен на существующей VM. Подтверждённая работающая
версия `aeea6f591afcbd17f8d649d639652bf5b3cbcbe4`; финансовая база
`/var/lib/tradingbot/paper-financial/shared-paper.db`. Подробная сверка, текущий
статус, копии и команды эксплуатации — в начале HANDOFF.md. Ниже сохранена
история разработки: её прежние формулировки о неустановленном состоянии и
старых позициях исторические, а не актуальные инструкции. Используются только
paper-режимы Spot и isolated futures. Старые финансовые базы сохранены root:0400;
не использовать их как действующие и не восстанавливать поверх новых событий.

# Paper bots on VM: migration and operations

On 2026-11-01 the user authorized migration of the existing paper bots to `tradingbot-paper-test` (`europe-west1-b`), without changing parameters or starting live/Testnet orders. Cloud Shell entries paused 12:29:18 Baku; both processes stopped. Before VM startup three independent SQLite backups were created, integrity checked and transferred with matching SHA-256. Original DBs and snapshots remain on Cloud Shell. Existing checkout directories were renamed to `Trading_Bot_paper_observation_MIGRATED_DISABLED` and `Trading_Bot_futures_paper_MIGRATED_DISABLED`; marker-only directories replace old launch paths. Process scans found no old trading processes; cron inspection found no matching launcher and no matching user systemd service. This prevents accidental execution of old documented paths, not malicious/manual startup or distributed fencing.

VM checkout `/opt/trading-bot-paper` is `44776b21aed04776f54d4652a653248f147e5c50`, with `deploy/paper_vm_backup.py` added separately for backup. Runtime user `tradingbot` owns private state under `/var/lib/tradingbot`. Databases:

- Spot: `/var/lib/tradingbot/Trading_Bot_paper_state/runtime-paper.db`.
- Futures: `/var/lib/tradingbot/Trading_Bot_futures_state/futures-paper.db`.
- Observation: `/var/lib/tradingbot/Trading_Bot_paper_state/monitor/observation.db`.
- Local backups: `/var/lib/tradingbot/paper-backups/{spot.db,futures.db,observation.db}`. Each is a separately consistent SQLite snapshot, not a simultaneous cross-database snapshot. Backups remain on the same disk and do not protect against its loss.

Services: `tradingbot-spot.service`, `tradingbot-futures.service`, `tradingbot-monitor.service`, `tradingbot-paper-backup.timer` and its oneshot service. The corresponding templates in `deploy/vm-*.service` are installed under the names above; the separate paper backup service/timer are also in deploy. The monitor additionally maintains Spot backup and text/JSON reports in its state directory. Backups are hourly and shortly after boot. The protected Telegram configuration is private to the service account; never print it. Spot has the sole polling process and holds the futures delivery lock. Two distinctly labelled test messages were queued and removed on acknowledged Telegram sends; user reading them was not verified.

Spot 15m, up to 10 virtual USDT; futures 15m, isolated 1x, margin 10 virtual USDT, 5 bps fee and 2 bps slippage. On the last recovery the same futures SHORT `0.0001290910503597050085467079348 BTC` remained open, funding `0.002006831540511705326085906338 USDT`, two funding events, no forced close. Position and funding may subsequently change through ordinary paper risk management. Spot was flat and had no pending orders. Both bots started on pause, fresh prices and no blocks/errors; after actual reboot they returned on pause with position/funding intact. User-authorized paper entries were resumed at 12:34:29 Baku. Initial observation/entry interruption was ~5m11s, including the reboot test. Paper risk management of an open position continues while paused.

Full Linux suite 247/247 was run in the same code checkout during VM qualification; migration-specific checks verified DB hashes, integrity, relay, notification queues, services, reboot and backup. Under the short real three-database backup with both bots/monitor running, minimum sampled MemAvailable ~585 MiB; spot/futures/monitor RSS ~27.3/27.1/23.3 MiB. This was a short sample, not long-term peak or stress proof. No paid capacity was increased.

## Operational commands, on the VM

Connect through the pinned IAP SSH instructions in [VM_TEST_SETUP_2026_11_01.md](VM_TEST_SETUP_2026_11_01.md). Do not print configuration or process environments.

```bash
sudo systemctl status tradingbot-spot.service tradingbot-futures.service tradingbot-monitor.service tradingbot-paper-backup.timer --no-pager
sudo cat /var/lib/tradingbot/Trading_Bot_paper_state/runtime-health.json
sudo cat /var/lib/tradingbot/Trading_Bot_futures_state/futures-health.json
sudo systemctl start tradingbot-paper-backup.service
sudo journalctl -u tradingbot-paper-backup.service -n 20 --no-pager
```

Spot `/pause` and `/resume` operate through Telegram. To stop a paper bot safely (open paper positions are not force-closed), use `sudo systemctl stop tradingbot-spot.service` or `sudo systemctl stop tradingbot-futures.service`. Stop both with both unit names. `sudo systemctl start <unit>` starts it **paused**. A later VM reboot or service restart also starts paused. After explicit authorization, fresh health, no errors or recovery blocks, known position and no outstanding Spot orders, allow entries separately:

```bash
sudo systemctl kill --kill-who=main --signal=USR1 tradingbot-spot.service
sudo systemctl kill --kill-who=main --signal=USR1 tradingbot-futures.service
```

Verify subsequent health shows `paused: false`; do not send signals merely because the systemd service is active. Stop/restart removes continuous risk monitoring during the outage. Never start the saved Cloud Shell copies while VM copies run. Network loss, VM shutdown, single-disk failure and lack of distributed fencing remain availability risks.

## Unified Telegram paper control — deployed 2026-11-01

In the new implementation `/mode` shows the selected market, `/mode spot` and `/mode futures` select it without stopping either worker. `/status` requests a short status from both with separate quote timestamps; `/status spot` and `/status futures` request detailed reports. `/signal`, `/history`, `/pause`, `/resume`, `/timeframe` apply to the selected market; `/leverage` applies only to futures. `/close` proposes the current position and size, and gives a short-lived `/confirm ID` and `/cancel ID`. Confirmation is bound to that market, position, quantity and expiry; it is rechecked at execution. `/pause_all` tracks each market separately. A temporarily unavailable worker may delay its reply while the other continues. These are paper-only controls, not real or Testnet trading.

Both worker services require `UNIFIED_PAPER_QUEUE=/var/lib/tradingbot/Trading_Bot_paper_state/unified-commands.db`; only Spot polls Telegram. The backup service includes the queue. Backups of separate SQLite files are individually consistent, not a coordinated cross-database recovery point: do not restore one database in isolation and assume command acknowledgements match another. Plan coordinated restore and reconciliation. The queue must be retained with the trade databases; losing it loses routing/confirmation history.

Installed checkout `/opt/trading-bot-unified` uses code commit `4e60b1cfd78f084cd085255434d4d321edda1198`; systemd working directories point there. Pre-update snapshots and prior systemd units are in `/var/lib/tradingbot/before-unified-20261101-130427`. During cutover 13:04:27–13:06:20 Baku both paper entries were paused. Position, funding and parameters survived; both previously enabled entries resumed after health checks. One Spot Telegram poller and the futures relay lock were checked, and an outgoing test message was acknowledged; live inbound user command delivery has not yet been independently confirmed. The systemd names and stop/start commands above remain valid. The 271-test Linux suite passed on WSL and the VM. After any worker restart, old queued dangerous commands are rejected and entries start paused.
