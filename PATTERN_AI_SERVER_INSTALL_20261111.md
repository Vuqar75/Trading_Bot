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

Follow-up before the first paid boundary: paid mode now requires an explicit
single aligned boundary; its free-only deployment template cannot accidentally
enable an unlimited paid schedule. Two targeted boundary/argument tests passed
on the VM. The existing AI report's one-boundary display explicitly states
that later paid requests are disabled, while local technical updates continue;
this was checked using an isolated mocked journal and a targeted test. The
2026-11-11 15:30 UTC candle produced and published a fresh local snapshot at
15:31:25 UTC with no GPT call. The actual Telegram callback path passed an
in-memory authorization/replay/no-financial-job check under the bot user.
On the VM a separate free-only capture and analysis measured peak RSS 83,644
KiB, below the configured 128 MiB service limit. A no-network transient probe
confirmed the existing encrypted credential is made available to `adaptiveai`
without displaying its value.

## First actual enriched GPT decision

At the authorized 16:00 UTC boundary, actual request
`regular:1794412800000` used the saved snapshot
`c04c8553195374d619d6d5594bb34b1ef8849053b9fb870b1ae9a00ca9058d6b`
and technical packet
`5d9c91c1219eba346fd329cfc9a560371eb3ca699643ea5d6fd680614ed2e290`.
Data time was 16:02:26.071 UTC, response time 16:02:43.409 UTC and expiry
20:00 UTC. GPT chose RANGE, and the baseline for that same snapshot chose
RANGE. GPT cited weak 4H direction and ADX, mixed momentum, absent detected
patterns and the absence of an executable quote; it did not authorize a trade.
The stored 16:00 UTC local technical display showed RSI 57.29, ADX 17.67,
ATR 0.3749%, no confirmed pattern and no trade. The model is research-only;
historical qualification is false.

The single request has exactly one send claim, one durable response and a
completed decision, including usage 1,295 input and 894 output tokens. Its
reserve is 1,902 micro-USD. At the existing executor's rate assumptions the
estimated incremental cost is $0.0005765; this is neither an additional
reservation nor a confirmed provider invoice. The journal contains eight
attempts and $0.018370 reserved; remaining limit $0.231630. Two previous actual
charges remain unknown. Repeated 16:03 and 16:04 service ticks reported the
existing completed attempt and did not generate a second claim.

The root publisher published both fresh technical and GPT reports. `/ai`,
`/ai patterns` and `/ai costs` were rendered under `tradingbot`, including
the correct one-boundary paid schedule; this verifies formation, not owner
receipt. A post-attempt external backup was uploaded, downloaded, decrypted
and restored into an isolated temporary database by the existing service at
16:04:05 UTC. Its ciphertext SHA-256 is
`a839ddd4fde6998178f6a493e8e7089dc9dc1b239d3a2b263e7010853615c75d`;
it verified eight attempts and 18,370 micro-USD reserved. It remains host-bound.
Post-check: all four working markets paused, no pending queue jobs, one paper
main/Telegram process. The local-analysis timer remains active, but its paid
gate cannot authorize another boundary.
