# Runtime validation — 2026-10-31

- `python3 -B -m unittest discover -s tests`: 199/199 passed, 2.923 seconds.
- Discovery before the final rejection test: 198 tests, 198 unique IDs. Final new
  rejection test brings the total to 199, with no inherited fixture duplicates.
- `python3 -B -m trading_bot.app --demo`: two orders, quantity 0, net 0.0859000,
  no pending order. No network, environment credentials or legacy files used.
- `systemd-analyze verify` on paper, backup and timer units: exit 0.
- `git diff --check`: clean.
- Public `GET https://testnet.binance.vision/api/v3/time`: HTTP 451.
- Live-data `--once` on a temporary dedicated DB: exit 1, paused true,
  pending 0, no quote, redacted error status written as intended.
- No real signed Testnet requests, Telegram requests, VM provisioning or deployment.
- No gcloud executable or configured managed GCP outbound identity is available.

New runtime tests cover paper exact fees, unknown submit recovery, cumulative
partial fills, fill replay/conflict rollback, stale/pause claim checks, position
identity rotation, unknown fees, schema/account identity guards, command and
fill process crashes before/after commit, owner/private-chat/staleness checks,
reply failure, process lock, backup integrity, protective exits without candles,
full signal tick, two SQLite writers, budget cap, exchange filters and signed
Testnet adapter end-to-end on an injected fake transport including response loss.

This is implementation and local verification, not independent review, live API
certification, performance evidence, profitability evidence or production approval.
