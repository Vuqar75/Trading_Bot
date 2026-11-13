# AI → limit entry → TP/SL → isolated paper lifecycle

Owner requested this path on 13 November2026. Scope: extend the existing project,
not restart it, not claim a profitable strategy. Existing failed ML models stay
unqualified; their flags are not repurposed to authorize this separate policy.
No working market is unpaused. No real/Testnet order adapter exists here.

The analyst chooses LONG/SHORT/WAIT and entry/SL/TP among identified, confirmed
technical levels, with multi-timeframe context and explicitly incomplete news
coverage. It cannot choose quantity, leverage, widen risk or issue executable
code. Programmatic checks bind the response to its exact immutable snapshot,
check freshness, tick/lot/minimum constraints and net reward/risk >=1.5 after
fees, stop slippage and funding buffer. Fixed research limits:100USDT notional,
1x,5USDT estimated stop risk, one active plan acrossBTC/ETH/SOL,1000virtualUSDT.
A stop is not a guaranteed maximum loss on a gap.

One private SQLite contains pending entry, partial fills, position, protective
orders, input idempotency, immutable double-entry accounting and status outbox.
Reuse PaperWalletLedger; do not patch old working handlers to bypass their gates.
Limit fills require a later public aggressor trade STRICTLY THROUGH the price;
a candle touch is never a fill. Quantity is bounded by observed trade volume,
rounded down to step. This is an explicit counterfactual paper model, not proof
of real queue priority. Fee5bps/side (no guaranteed maker discount).

SL triggers on fresh mark OR adverse executable bid/ask, closes using fresh bid/ask with10bps adverse slippage,
limited by visible executable quantity. Unfilled residual remains open under
EXIT_PENDING. TP is reduce-only, resting limit with same trade-through rule.
Any exit cancels remaining entry. No reversal, pyramiding or silent replacement
of an active plan. Entry expiry/pause cancel only remaining entry; position
protection, manual close and4hmaximum hold remain independent of the analyst.

Funding is charged to the quantity actually open at its event time, even when
reported after an exit. Physical protection is not blocked by missing funding;
financial completion remains provisional and new entries blocked until coverage
through the close is confirmed. No missing funding is silently called zero.
Buffer0.0021notional is retained until full reconciliation. Revisions block new
entries; already accepted events are not posted twice. Restore preserves IDs,
orders, cash and outstanding protection.

LLM transport must reuse ONLY existing server-owner budget/credential and global
$0.25 cap, max2000output tokens/reserve$0.003640, shared executor lock. No new
active budget or credential. Persist reservation/send-claim BEFORE HTTP and raw
reply BEFORE parsing/usage; uncertain sends never retry. New purpose must be
explicitly selected; old timers and old lock files are not reset. Refreshing a
Telegram view does not call the model. Live funding/filters/quote evidence and
an actual new model answer are needed for deployment acceptance; fixture demos
must never be presented as profitable strategy trades or live AI responses.

## Implemented candidate, 13 November2026

Modules ai_limit_plan/book/executor/feed/runtime implement the complete isolated
paper path. Source levels are confirmed two-bar swings on closed15m/1h/4h;
SL is placed BEYOND the selected invalidation level by max(one tick,0.1ATR),
TP BEFORE its selected opposing level by the same padding.5m is explicitly
unavailable in this15m source; it is not synthesized. ATR serialization is
bounded to8decimal places. Facts about OI, positioning and taker activity come
from existing derivatives_context, with its availability lag/missingness labels.
Fed/ECB headlines are retained as untrusted, incomplete fundamental context.
A missing calendar is never described as complete fundamental coverage.

Paper actor and analyst are separate processes/users. Model latency requires a
NEW fresh executable quote at admission; it cannot silently reuse the old quote
sent to the model. Empty book or mark/bid divergence cannot disable a protective
exit. A zero-liquidity exit remains pending. Repeated mark updates do not reuse
liquidity from the identical book. Trade IDs replay idempotently even when
received timestamps differ. Gaps cancel unfilled entry and never backfill fills.

A completed but unsuitable model answer is saved with usage as REJECTED, not
retried. A reservation without a durable answer blocks further sends; local
recovery of saved replies needs no HTTP. Credential and usage-write preflight
occur BEFORE reservation. No private key is requested in chat/Windows/WSL.
Fixture budgets contain TEST_ONLY and cannot call real HTTP; the live ingest
rejects fixture provenance. UI explicitly labels every synthetic example.

Funding coverage means all CASHFLOWS of actual account exposure in the covered
window, not proof of every exchange event for symbols with no positions.
Unrelated symbols with no exposure are not dependencies of position protection.
Previously observed due times must be found in returned settlement history
before coverage advances. Missing/revised funding prevents final financial
completion/new plans, while fresh-price exits continue. Original account
history remains immutable. Gap and exchange liquidation behavior remain paper
approximations; this implementation is NOT an adapter for real-money futures.

`/ai orders [BTCUSDT|ETHUSDT|SOLUSDT]` shows this separate account, its actual
paper fill quantity, unfilled entry, TP/SL, provisional/final result, source
freshness and API estimate. It is read-only; it neither calls the model nor
changes pair/pause. The old rejected ML model stays blocked and old working
market commands keep their own meaning. Emergency pause/close of this new
account is via the dedicated runtime CLI as its OS user, not legacy `/close`.

66 affected Linux tests passed, including existing queue/asset/screen tests.
`systemd-analyze verify` passed for the three provided units; no actual systemd
service/reboot was tested in this cloud container. The synthetic end-to-end
trace covers source→request→test transport→sole test journal→publisher→entry
partial fills→LONG TP/SHORT gapSL→WAIT→funding completion→restore→Telegram view.
It used ZERO real model calls/real/Testnet/working paper orders. Its deliberately
constructed prices/outcomes are NOT a strategy backtest or evidence of profit.

## Runtime and ownership

`/opt/tradingbot-ai-brackets` is a read-only pinned checkout of this candidate.
Create OS user aibracket and directories:

- `/var/lib/ai-bracket-study`:0700, owner aibracket (new virtual ledger only).
- `/var/lib/ai-bracket-public`:0755, owner aibracket (published orders.json0644).
- Existing adaptiveai server budget/credential locations remain unchanged.

Paper service has NO credential and cannot read the original budget, working
financial stores or credstore. Analyst reads only the public paper status and
uses the existing systemd credential and journal under adaptiveai. No IAM
extension or new cloud resource. Public endpoints stay on the original allowed
Binance host; HTTP401/403/451 latches denial, no alternate-host bypass.

The first deployment enables ONLY the isolated new paper account/BTC pilot.
The code supports3symbols, default service pollsBTC plus any active symbol;
adding assets requires including them in serve --symbols and selecting that
asset for its bounded analyst opportunity. There is still ONE global position.
The4H analyst timer runs00/04/08/12/16/20:02UTC (04/08/12/16/20/00:02Baku),
max6attempts/day, >=1hpaid cooldown, remaining ORIGINAL$0.25reserve cap.
It skips paused/busy/invalid-data states. Old paid timers stay off. The new
paper account cannot re-enable itself after an operator pause on restart.
Enable the timer only after one genuine, complete and published first response
or WAIT. Never create a trade to satisfy an acceptance checklist.

Local hourly backups retain24files, using SQLite backup API and integrity_check;
full restoration is tested by the synthetic acceptance. Before live rollout,
add this new SQLite to the EXISTING verified external-backup mechanism. Its
source captures/fills/orders are inside SQLite. The original budget SQLite
backup includes new request/response tables automatically; credential storage
and host-bound encryption retain their earlier limitations.

Useful commands (run as aibracket, with PYTHONPATH pointing to pinned checkout):

```bash
python3 -B -m research.ai_limit_runtime status --state /var/lib/ai-bracket-study
python3 -B -m research.ai_limit_runtime pause --state /var/lib/ai-bracket-study
python3 -B -m research.ai_limit_runtime resume --state /var/lib/ai-bracket-study
python3 -B -m research.ai_limit_runtime close --state /var/lib/ai-bracket-study --plan-id ACTUAL_ID
python3 -B -m research.ai_limit_runtime backup --state /var/lib/ai-bracket-study --target NEW_BACKUP_PATH
```

No VM deployment was performed here: this environment has no gcloud command,
existing authorized VM connection or original server budget. This is a concrete
access boundary, not a request for new owner authorization/key. The attached
local rollout task completes the server step through the existing local access.

Before asking the model, the program provides at most four mechanically valid
LONG and four SHORT alternatives (deduplicated level prices). Each includes
computed entry/SL/TP, size/risk cap and cost-adjusted R/R. The model selects one
exact source-ID triple or WAIT based on the technical/fundamental evidence.
It cannot select a different unvetted triple. These alternatives are not a
profit forecast or a revival of previously rejected ML strategy permissions.

Initial activation is atomic and cannot override an operator pause, including a
pause recorded before the very first service start. Explicit resume never
clears a funding conflict or financial block.
