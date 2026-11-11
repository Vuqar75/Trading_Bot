# AI trade scenarios — installed 2026-11-12

Installed runtime from `fa16c3c4286b004cc2bf5ea3f5b76f0ba19604dd` on the
existing paper VM. The prior installed server, publisher and bot files matched
the exact parent candidate after CRLF normalization. The new pure-Python
scenario module is available in all three runtime paths:

* `/opt/tradingbot-scenarios-fa16c3c-server`;
* `/opt/tradingbot-scenarios-fa16c3c-publisher`;
* `/opt/tradingbot-scenarios-fa16c3c-bot`, bot integration commit
  `74205ace0b91bca8d53089cea7e8ab4bf2d6d948`.

The existing timer and publisher are reused. Their previous drop-ins are
retained under `/var/lib/tradingbot-scenarios-rollout-fa16c3c`. Only the Spot
process that owns the single Telegram poller was restarted, after checking all
four pauses, zero Spot quantity and pending actions, coordinated backup and a
timed rollback. The bot-unit recovery state is retained under
`/var/lib/tradingbot-scenarios-view-rollout-fa16c3c`. Other trading processes,
the independent observer and the stopped study were not restarted.

The 2026-11-11 21:30 UTC / 2026-11-12 01:30 Baku candle produced a normal
free snapshot at 21:31:30.856 UTC. Packet ID
`c3587ecd1dd772aab93b3e84323ae8c837021e7af488be7181f18fd22695f042` was
published by the existing root publisher. The actual signal was WAIT with no
unambiguous pattern. `/ai patterns` and `/ai plan` were rendered as the bot
user; the plan explicitly states that no levels are formed. The button to the
plan and back, unauthorized callbacks, replay idempotency and absence of
financial jobs were checked using an isolated in-memory queue. This verifies
message formation, not owner receipt; no Telegram message was sent for testing.

For a previously saved real 21:15 UTC candle with LONG `pin_long`, close
74672.3 USDT and ATR 0.002920184371306097, the scenario's 0.5% clipped stop,
entry, and base/stress target and stop net values were independently checked
with Decimal arithmetic. A separate saved real WAIT snapshot confirmed that
no pattern means an empty scenario list and no fabricated levels. These were
read-only checks; no new pattern or position was created.

All four working markets remain paused; one paper/Telegram main process and
no pending unified jobs were observed. The study and Donchian remain
inactive/disabled. The model file retains byte SHA-256
`cc3ee2473b4ba5c24bd7f555b47acf4d32cdfe72f414aabc89c744bb7afa8277`.
The single budget still has eight attempts and 18,370 micro-USD reserved;
no new paid attempt. The existing free-analysis timer remains active with the
single-paid-boundary guard. The bot cannot read the budget or write the public
snapshot. External encrypted budget backup was read back and restored by the
existing mechanism at 21:33 UTC, SHA-256
`56f6a553e4815ac41bf5f86cdf340ce7424f63a525a9c3aa82efa0c78dcc4acf`;
it is host-bound. The pre-install financial/queue/observer backup was
`paper-20261111T212143741365Z`.

The displayed arithmetic is conditional, normalized to 100 USDT and based
on a closed candle, not an executable quote or approved order size. Funding,
gaps, time exits, fills and filters remain unverified; strategy profitability
and automatic entries are not established. The prior 153 checks were not
repeated because the integrated runtime code was not modified.
