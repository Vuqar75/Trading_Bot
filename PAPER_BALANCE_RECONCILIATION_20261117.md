# Exact paper balance reconciliation —17November2026

**The decrease was one BTCUSDT paper SHORT closed by SL before installation.**
It is not an installation charge or a balance reset. Normal runtime b36103c was
installed14:44Baku; all four explanatory ledger events occurred00:02–00:27Baku.
[Exact saved evidence](research/PAPER_BALANCE_RECONCILIATION_20261117.json).

Read-only collection 2026-11-17T15:33:05.968000+04:00–2026-11-17T15:33:08.475000+04:00. Saved live database opened mode=ro in one read
transaction; prior completed snapshot opened mode=ro&immutable=1. No Book/live
executor created, SQL writes, model/API requests, trades, retries, timers,
settings/service changes, reinstall or full test suite. Old snapshot:
/var/lib/ai-bracket-study/backups/1794856562431.sqlite,
SHA5a5bb9cdeef7d5bede10d9587bb2634e1fbd487ef4b1f85a6fa5b35e3afd0178.
Its24events,4fills,9plans and funding history match the live prefix exactly;
ledger reconciliation passes for both. No accounting error found.

## Position and actual recorded result

Plan ID:
`48b56f255f17a2e6cb496e3cc3ae786ccbd0d7604a09b47332bd838ea5aeda48`

SHORT0.001BTC, entry65048.2, configured SL65187.5, TP64098.4USDT.
Entry reason LIMIT_TRADE_THROUGH, exit reason SL. Stored ask65194.80 crossed
the SHORT stop; SL observed00:27:29.872Baku and paper exit recorded00:27:29.875.
Exit model uses the saved10bps adverse impact:
`65194.80 * 1.001 = 65259.9948`.
The0.0651948USDT modeled impact is ALREADY inside gross and is not deducted
again. These are paper fill prices/times, not real exchange orders or later
REST reconstruction. Position fully closed; accounting finalized00:27:45.975.

| component | exact USDT |
|---|---:|
| SHORT gross:0.001*(65048.2-65259.9948) |-0.2117948|
| Entry fee:0.001*65048.2*0.0005 |-0.0325241|
| Exit fee:0.001*65259.9948*0.0005 |-0.0326299974|
| Actual funding for this position |0|
| Net |-0.2769488974|

The stored plan charges5bps on each side; limit trade-through does not imply a
maker discount. No new FUNDING posting or funding-history event appeared in
this interval. FUNDING_FINALIZED below returns an unused buffer, not funding
income/expense. No additional account movement is needed to explain the change.

## Complete new double-entry postings

Times below are ledger-created times in Asia/Baku (UTC+4); all17November2026.
Exact raw created seconds and saved fill stamp milliseconds remain in evidence.

### A — 2026-11-17T00:02:45.164+04:00 — LIMIT_RESERVE

ID: `48b56f255f17a2e6cb496e3cc3ae786ccbd0d7604a09b47332bd838ea5aeda48:reserve`

| account | asset | signed amount |
|---|---|---:|
|ai_bracket:free|USDT|-65.21732532|
|ai_bracket:reserved|USDT|65.21732532|

Each event balances exactly to0USDT.

### B — 2026-11-17T00:03:19.301+04:00 — PAPER_ENTRY

ID: `trade:BTCUSDT:3528394331:entry`

| account | asset | signed amount |
|---|---|---:|
|ai_bracket:reserved|USDT|-65.21732532|
|ai_bracket:margin|USDT|65.0482|
|ai_bracket:fees|USDT|0.03252410|
|ai_bracket:funding_buffer|USDT|0.13660122|

Each event balances exactly to0USDT.

### C — 2026-11-17T00:27:29.875+04:00 — PAPER_EXIT

ID: `market:48b56f255f17a2e6cb496e3cc3ae786ccbd0d7604a09b47332bd838ea5aeda48:3293ac0edca6b39bcf8009963365bf22db35caa6141b3e8d2bb745f21f88a1c8:0.001`

| account | asset | signed amount |
|---|---|---:|
|ai_bracket:margin|USDT|-65.0482|
|ai_bracket:free|USDT|64.803775202600|
|ai_bracket:fees|USDT|0.032629997400|
|external:price_pnl|USDT|0.21179480|

Each event balances exactly to0USDT.

### D — 2026-11-17T00:27:45.975+04:00 — FUNDING_FINALIZED

ID: `48b56f255f17a2e6cb496e3cc3ae786ccbd0d7604a09b47332bd838ea5aeda48:release-buffer`

| account | asset | signed amount |
|---|---|---:|
|ai_bracket:funding_buffer|USDT|-0.13660122|
|ai_bracket:free|USDT|0.13660122|

Each event balances exactly to0USDT.

`external:price_pnl` is the balancing price-result account within the paper
ledger, not an external financial transfer. Fees are expense-account credits.
Entry fee is paid from already reserved money, so the ENTRY event itself has
zero additional free-cash movement.

## Cash path, without double counting

| event | change in ai_bracket:free | free after event, exact USDT |
|---|---:|---:|
|A|-65.21732532|935.0917706878653898110|
|B|0|935.0917706878653898110|
|C|64.803775202600|999.8955458904653898110|
|D|0.13660122|1000.0321471104653898110|

Exact cash-posting reconciliation:

```text
1000.3090960078653898110
 -65.21732532
  +0
 +64.803775202600
  +0.13660122
=1000.0321471104653898110 USDT
```

Equivalent economic reconciliation:

```text
1000.3090960078653898110
  -0.2117948          gross loss including modeled exit impact
  -0.0325241          entry fee
  -0.0326299974       exit fee
  +0                 actual funding
=1000.0321471104653898110 USDT

Exact balance delta = -0.2769488974000000000 USDT
```

All computations use Decimal precision120, no float summation/tolerance.
Reserved/margin/funding_buffer deltas each0. Fees account rises exactly
0.065154097400USDT; price-result counterpart0.21179480USDT. No duplicated
posting, unexplained remainder or corrective financial action. The older
1000.309096 and newer1000.032147 figures were shortened displays; preserved
ledger precision is shown above. Installation pre/post financial hash was
unchanged; this loss had already been included in its preflight balance.
