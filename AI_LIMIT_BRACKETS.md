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

SL triggers on fresh mark, closes using fresh bid/ask with10bps adverse slippage,
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
