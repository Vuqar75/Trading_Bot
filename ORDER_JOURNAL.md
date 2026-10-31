# Offline order journal: stage 2, block 1

This isolated module is not wired into the bot. It submits only to the supplied
simulator and does not import credentials, Telegram, Engine or the HTTP adapter.
No deployment or live database migration is part of this block.

## Reproduce

Use a separate checkout and Python 3.10 or newer; no new dependencies are needed.

```sh
python -B -m unittest discover -s tests -p test_order_journal.py -v
python -B tools/order_fault_harness.py
```

The harness creates temporary journal and simulator databases, terminates child
processes at commit boundaries, reopens each journal and checks that no second
submit occurs. Sockets are blocked in parent and child harness processes. Do not
use Python's `-O` option: the harness uses assertions. Optional `--output report.json`
writes a synthetic JSON report. No bot configuration or exchange account is needed.

For full regression, run `python -B -m unittest discover -s tests -v` on Linux.
The recorded result is 81 passing tests, including 17 new tests; the standalone
fault matrix passes 19 scenarios. See `research/stage2-block1-validation.json`.

## State protocol

| Durable state | Meaning | Dispatch permitted? |
|---|---|---|
| PREPARED | Intent and initial event committed | First claim only |
| SUBMITTING | Send right claimed before calling simulator | No |
| ACKNOWLEDGED | Valid response stored, not settlement | No |
| REJECTED | Definite simulated rejection stored | No |
| UNKNOWN | Submit exception or invalid response | No |

`prepare(namespace, business_key, Request(...))` creates or returns the same intent.
Changing the request under the same key is an error. Amounts are positive decimal
strings; the current contract is BTCUSDT spot MARKET only. IDs are generated once.
Caller-supplied keys must be internal, non-personal identifiers: never pass raw
messages, account identifiers, secrets or Telegram IDs.

Every state update and its event share a `BEGIN IMMEDIATE` transaction. SQLite
serializes competing claims; `synchronous=FULL` is requested. Events cannot be
updated or deleted through ordinary SQL because of append-only triggers. Schema
v1 bootstraps transactionally and rejects foreign tables or unsupported versions.
The simulator stores calls in a separate database and deliberately accepts duplicate
client IDs, making duplicate coordinator submissions observable.

## Failure semantics and limits

A crash before claim commit rolls back and permits the first send. A crash after
claim commit can mean either zero sends or an accepted order with no stored reply.
Both leave SUBMITTING blocked. Caught submit failures become UNKNOWN. Reopening
does not reset either state; there is no resend/reset/reconciliation API here.
Failure while saving a receipt leaves SUBMITTING or an already committed outcome.

This guarantees at most one coordinator submit per durable intent, not exactly-once
exchange execution. New business keys, deleted databases, direct simulator calls
or bypassing the coordinator are outside that guarantee. The simulator marker is
an interface guard, not a security boundary for arbitrary third-party code.

Crash testing covers process termination and transaction rollback, not physical
disk loss or power failure. Observed FILLED/PARTIALLY_FILLED status does not update
positions or account for fills. Real transport, reconciliation, fees, residuals,
Telegram command deduplication and whole-bot process ownership remain unimplemented.
Keep UNKNOWN blocked until a separately approved reconciliation block exists.

Read `HANDOFF.md` for preserved research results, decisions and open questions.
