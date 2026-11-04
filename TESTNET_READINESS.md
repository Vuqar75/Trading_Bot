# Spot Testnet: read-only integration and VM preparation

This checkout is separate from the running bot. No strategy, Telegram polling,
order submission, cancel, gate clearing or VM creation is enabled here.

## Run

Public quote, no credentials:

```sh
python3 -B -m trading_bot.testnet_observer --once --report /tmp/observer.json
```

For an existing Testnet order, configure BINANCE_TESTNET_API_KEY and
BINANCE_TESTNET_API_SECRET outside the repository, then add `--client-id ID`.
Never use production credentials. Reports contain order IDs and fee amounts;
keep them private and out of Git. Reports are atomic replacements, mode 600 on
creation. Failures replace prior success with ok=false and a redacted error type.
A failed --once exits 1. Continuous mode repeats at --interval (default 60s).

The adapter permits only GET to a fixed Testnet host and an endpoint allowlist.
It reads server time before signed requests, never logs response error bodies,
and never infers an absent order was not sent. Order audit fetches the order,
paginates myTrades by trade ID, and rereads the order. Changed observations,
missing totals, inconsistent sides/statuses or exhausted pagination fail closed.
Fees stay in their original assets; no fictional third-asset valuation is made.

## Important boundaries

This is NOT a real-exchange RecoveryExchange. Matching cumulative totals for one
order does not prove account-wide history completeness, balances, absence of
external orders, or atomic snapshots. No observer report can clear the offline
gate. No ledger mutation is performed. Quote-budget original quantity semantics,
cancel/expire, Testnet resets, filters and future submission contracts still need
real payload validation before enabling order submission. Signed endpoints have
only mocked coverage in this environment; no credentials were read or used.

The current service monitors PUBLIC quotes only, not balances or open positions.
It does not protect a trading position and must not replace the running bot.
HTTP failures retain no raw diagnostic body, so troubleshooting may require a
separate sanitized transport diagnostic. Report age must be checked by consumers.

## VM preparation (files only; not installed)

`deploy/trading-bot-observer.service` runs only this observer, as tradingbot,
with systemd restart and a persistent StateDirectory. On a separately approved VM:

1. Install Python 3, create the dedicated tradingbot system user/group.
2. Put a pinned, reviewed checkout at /opt/trading-bot, root-owned/readable.
3. Optional credentials: /etc/trading-bot/observer.env, root-owned 600,
   variables above; never put Telegram settings there for this observer.
4. Install the unit in /etc/systemd/system and daemon-reload.
5. Enable/start this unit only after explicit deployment approval.
6. Inspect journalctl -u trading-bot-observer and status.json timestamp/ok.

No unit install or server creation was executed. This does not constitute the
final trading service, backups of trading SQLite, monitoring alert delivery,
exclusive trading process lock, or recovery before dispatch. Those belong to the
later approved execution runtime. systemd restarts do not prove safe recovery.

## Validation in this delivery

170/170 Linux tests passed (162 inherited + 8 adapter tests). Added cases cover
signed GET, missing fills, changed order, contradictory partial status, side and
identity mismatch, pagination bounds, credential/endpoint guards, report replace
and error redaction. No new order was sent.

Public Testnet smoke was attempted without credentials; the request failed and
the observer correctly wrote ok=false / ObservationError. Thus actual API
connectivity and payload compatibility were NOT established by that smoke.
