# Pattern AI server installation — 2026-11-11

Deployment is analytic shadow only. No market entry, strategy admission, real
order, budget increase, new ledger or credential change is authorized.

The installed source is the e62bef8 runtime from `PATTERN_AI_DEPLOYMENT_MANIFEST.json`,
merged onto the actual installed bases. The AI executor base differs from the
manifest's base: only its `request` function was updated. The publisher and
Telegram files matched their stated base hashes. Live paths:

* `/opt/tradingbot-pattern-ecd5f88-server`; Python isolated in
  `/opt/tradingbot-pattern-venv` (numpy 2.3.5, pandas 2.2.3).
* `/opt/tradingbot-pattern-ecd5f88-publisher`, existing privileged publisher.
* `/opt/tradingbot-pattern-ecd5f88-bot`, actual bot integration commit
  `1065a5de9c283163408091c8e70d4de1216c06ea`, based on `8900dd2`.

On Nov 11 at 15:22 UTC, the existing regular service produced a current BTCUSDT
USD-M technical snapshot without GPT, and the existing publisher wrote its
validated copy to `/var/lib/tradingbot-ai-public/technical.json`. As the bot
user, reading the public snapshot succeeds and writing it or reading the
budget fails. A real public capture built a valid gpt-6-luna request containing
three input messages and technical context, without network model access.
The model's `orders_enabled` remains false and historical qualification is false.

The existing regular timer was enabled for free local updates. A new
`--paid-boundary-ms` guard allows paid execution only on the specifically
authorized 2026-11-11 16:00 UTC boundary (window 16:02–16:05 UTC). It does not
create a second executor, ledger or timer. Past and subsequent boundaries
cannot initiate a paid request through this configuration. Existing journal
idempotency, authorization, uncertainty gates and spending limits remain in
force. The paid attempt and its outcome must be verified from the journal;
neither a prepared request nor a scheduled timer is proof of a response.

Before switching, external encrypted budget backup at 15:15 UTC was uploaded,
downloaded and restored by the installed backup mechanism; its ciphertext
SHA-256 is `b257f3e6111127ccbc6054e2af8ead48c535ac616029cf31edb309d4058bf486`.
It contains seven attempts and 16,468 micro-USD reserved. Encryption is host
bound; restore on a different VM is not established. A new coordinated working
financial/queue/observer backup was also created. No SQLite copy is an active
writer.

Only the Spot service needed a restart for the Telegram renderer. Before and
after, all four working markets were paused, Spot quantity was zero and there
were no pending orders, queue jobs or confirmations. A bounded watchdog pause,
verified backup, independent timed rollback and post-restart checks were used;
the old code and data were not deleted. Spot PID after verified switch: 274613.
An actual bot-user in-memory queue test rendered `/ai patterns`, rejected an
unauthorized command and ignored a duplicate, without financial jobs. This
proves message formation, not owner receipt in Telegram.

Four focused actual-server prompt/compatibility tests and one one-boundary
guard test passed. The previously completed training, historical paths,
backtests and operator test trades were not repeated.
