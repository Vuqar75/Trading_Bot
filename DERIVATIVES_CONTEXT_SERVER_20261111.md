# Derivatives context — server installation, 2026-11-11

Installed the factual context from `f9f77da` on the existing paper VM without
new services, paid requests, training or trading. Only runtime files from
`DERIVATIVES_CONTEXT_DEPLOYMENT.json` were applied onto the actual installed
server, publisher and bot bases. The pre-existing `--paid-boundary-ms`
restriction is retained. New server, publisher and bot code directories are
`/opt/tradingbot-derivatives-f9f77da-{server,publisher,bot}`. The bot integration
commit, on top of installed `1065a5d`, is
`20204f20cc91e4239a72bf08004f600e8e7b03b8`; the modified view explicitly
shows open interest and both top-account and top-position ratios, with date.
Only the Spot process hosting the sole Telegram poller was restarted, after
coordinated backup and pause/queue checks, with independent timed rollback.
The other trading services were not restarted.

The unchanged active model remains at the earlier model path
`/opt/tradingbot-pattern-ecd5f88-server/research/pattern_ai_results/model.json`.
Its byte SHA-256 is `cc3ee2473b4ba5c24bd7f555b47acf4d32cdfe72f414aabc89c744bb7afa8277`;
normalizing CRLF to LF gives the manifest's
`3d0d99fe90766a57da48a9f1847dcffb5d330ce1e61260574e0597671fbe1c32`.
The candidate copy has the same byte hash. Calibrator and research forest
were not installed. A single public preflight capture was compared with and
without derivatives: the model features and output were identical. The GPT
request included the factual fields, kept the existing schema and 2,000-token
limit; its prospective reserve was 2,058 micro-USD. No model HTTP was sent.

The real 18:15 UTC / 22:15 Baku candle was processed and published at
18:16:26 UTC. Packet
`ca273c4c6091b8f505974b69ea31ea62db14e3e7519bd55640739d44a9214b1e`
contains complete available public metrics with cutoff 18:00 UTC / 22:00
Baku and source timestamps at that cutoff: OI 98,831.271 BTC; changes 1h
+0.06950%, 4h −1.62753%, 24h −2.13151%; global accounts L/S 2.6630,
top accounts 2.9339, top positions 2.2123 and taker BUY/SELL 1.0845.
These are delayed descriptive facts, not proof of direction, liquidations,
executable liquidity, model skill or profitable trades. OI and ratios use
period-end timestamps; taker source start is normalized to end. A missing or
blocked input remains partial/unavailable, never zero-filled.

The existing root publisher exposed only the validated sidecar; the actual
bot-user callback rendered the view without a financial job. Unauthorized and
repeated callbacks were checked in an isolated in-memory queue. At 18:17:20
UTC, a second tick returned `already_published` without another data capture
or paid request. The owner has not separately confirmed message receipt.

Fourteen focused candidate tests and two panel tests passed. The sole budget
still has eight attempts and 18,370 micro-USD reserved; no attempt was added
after the previously authorized 16:00 UTC boundary. All four working markets
remain paused, pending jobs zero, one paper/Telegram main process. The bot can
read but cannot modify the public snapshot or read the private budget. The
18:17 UTC external encrypted journal backup was uploaded, read back and
restored in an isolated temporary location with SHA-256
`aa50866d99fc3a088a1c52f6d19501dbe93e9815fe7b135270533b34414cb8a7`.
It remains host-bound and is not an independently proven cross-host restore.
Unit drop-in originals and rollback state are retained under
`/var/lib/tradingbot-derivatives-rollout-f9f77da` and
`/var/lib/tradingbot-derivatives-view-rollout-f9f77da`.
