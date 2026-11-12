# AI plan history — installed 2026-11-12

Installed the unchanged `9b05eb4cbdd3df339b4c0ec00df118fcc045fd15` runtime
on the existing VM. The actual five modified server/publisher/bot files
matched the candidate parent after CRLF normalization. Runtime directories:

* `/opt/tradingbot-history-9b05eb4-server`;
* `/opt/tradingbot-history-9b05eb4-publisher`;
* `/opt/tradingbot-history-9b05eb4-bot`, bot integration commit
  `7adce0b597f0b81ff5d1f47835209b59bb1ac2ff`.

The pure history validator exists in all three paths; the existing local
analysis, publisher, timer and single Telegram poller are reused. Only the
Spot process hosting that poller was restarted, after financial/queue checks,
coordinated backup and independently timed recovery. Original unit drop-ins
and rollback state remain under `/var/lib/tradingbot-history-rollout-9b05eb4`
and `/var/lib/tradingbot-history-view-rollout-9b05eb4`. No research service,
other trading process, model, paid boundary or budget lock was changed.

At 2026-11-12 03:46:26.000 UTC / 07:46:26.000 Asia/Baku, an ordinary free
capture created the history and root publisher exposed an identical validated
read-only copy. `started_ms=checked_ms=1794455186000`, SHA-256 of the JSON
`6789a0070c0b04dd70e65d76437d6c21f9b3fee2fa67663c11be6195c5838a82`.
There were zero records because the current signal was WAIT. Neither an old
plan nor a synthetic position was inserted. At 03:47:20 the existing timer
returned `already_published`, with no new capture or paid call.

Under the installed bot user, an isolated in-memory queue verified
`/ai plan` → `/ai history` → refresh, unauthorized callbacks, replay and zero
financial jobs, with network sockets blocked. The live screen displayed its
start time and an empty history, explaining that price observations are not
executions, trades, PnL or ML accuracy. This verifies message formation only;
owner receipt was not asserted. Previous 171 candidate tests were not rerun.

An independent host-encrypted one-time history copy was made using the existing
credential encryption and closed-bucket transfer/read-back mechanism, under
the separate object prefix `paper-backups/daily/ai-technical-history/`.
Downloaded ciphertext was decrypted into memory, its JSON and hash validated
against the locked source, and its bytes retained as a 0600 local encrypted
copy. Ciphertext SHA-256:
`da152901eb09650a31525664a7d14e327c6e1c4204123115d9fbeb2eeabcfc41`.
It is host-bound; restore on another VM is not proven. This is a **one-time**
copy: the existing periodic budget and trading archives do not include the
history, and no new backup timer was created.

Post-checks: four working markets remain paused; the single paper/Telegram
main process is active and the shared queue has no pending jobs. Study and
Donchian remain inactive/disabled. AI budget is unchanged at eight attempts
and 18,370 micro-USD reserved; the paid-boundary guard remains in place and no
new paid request was sent. The bot cannot read the budget or modify the
public history. The model hash remains
`cc3ee2473b4ba5c24bd7f555b47acf4d32cdfe72f414aabc89c744bb7afa8277`.
Historical levels are observations, not executed orders or profitability.
