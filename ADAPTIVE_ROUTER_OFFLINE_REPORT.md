# Adaptive router: offline bench

Scope: BTCUSDT, synthetic or saved inputs in temporary databases only. This is
an execution-mechanics experiment, not an approved strategy or a deployment.
The precommitted rules are in `ADAPTIVE_ROUTER_POLICY.md`. Run on Linux:

```sh
python3 -B tools/run_adaptive_router_bench.py
```

The runner creates a temporary home and temporary databases, removes secrets
from the environment, and blocks IPv4/IPv6 sockets. `--full` runs the full
application test discovery under the same isolation. No Telegram poller,
exchange order, AI request, or production database is used.

## What was exercised

* A saved decision and immutable input evidence enter the existing durable
  unified queue. Workers use the existing paper accounting/execution engines.
  TREND_UP exercises Spot LONG, TREND_DOWN isolated SHORT at 1x, RANGE the
  fixed Spot range rule, and NO_TRADE rejects new entries. Cross and margin
  are checked for existing exposure but have no adaptive entry route.
* Position, reservation, incomplete action, pending queue work, pause, and
  financial checks execute under the same SQLite write transaction as the
  Spot reservation/claim or isolated financial opening. The queue is attached
  to the shared financial database. Freshness and decision identity are
  checked again after lock acquisition. The separate route-state database has
  durable transitions and reconciliation; no cross-database atomic commit is
  claimed. No network operation is held inside these transactions.
* The tests exercise a real paper buy, monitoring, exit and crash recovery;
  a futures short, funding and exit; range target, conservative stop-first
  handling and timeout; stale data/decisions/filters after a real SQLite wait;
  competing/replayed work; a change of mode without changing an existing
  position's rules; actual confirmed manual closes; and backups restored on
  isolated copies after a fill before acknowledgement. Protection continues
  without a valid model decision when necessary market/funding data exist;
  missing data are shown as unavailable, not as healthy protection.
* Route state, queue jobs and financially applied events have distinct durable
  identities. Recovery reconciles a committed financial action before marking
  the queue/state complete; ambiguous outcomes remain blocked. Synthetic
  intrabar stop fills are conservative model estimates, not real exchange fills.

## Matched synthetic comparison

Both calculations use the same input fixture, initial 1,000 USDT, <=10 USDT
entry, and cost assumptions, in separate temporary accounts. Only the mode
source differs. In this fixture the synthetic AI mode TREND_UP enters and then
exits: net -0.329385374925 USDT, fees 0.019690299925 USDT, two paper orders.
The deterministic baseline selects RANGE; no range entry signal occurs and
its net is 0 with no orders. These are implementation checks, not evidence
of forecasting quality or profitability. `tests/test_adaptive_router_comparison.py`
records mode, route, signal, result, orders, costs, PnL and exposure separately.

## Limits and prerequisites for any later paper integration

All three research strategies remain economically rejected and have no
permission to trade. The isolated test adapter is deliberately not wired to
the production scheduler or live workers. The shared lock protects the tested
research handlers; it does not force unrelated legacy workers to invoke that
guard. Production integration would require a common admission boundary for
every writer, a trusted binding between saved model snapshots and input candles,
and verified public funding/market-data ingestion and coverage. The funding
evidence in this bench is synthetic and not proof of live source freshness.

Full paper fills are modelled; isolated partial closes and real liquidity are
not. A completed risk bar estimates a stop conservatively without reconstructing
intrabar order. The maximum-hold reference is the simulated entry boundary and
can precede the actual model fill by up to five seconds. Monitoring is invoked
by the offline harness; this report does not assert that a continuously running
protective scheduler exists. Financial, queue and route recovery were tested
on temporary copies, not against the working VM. Nothing here authorizes a
production installation or activation of these strategies.
