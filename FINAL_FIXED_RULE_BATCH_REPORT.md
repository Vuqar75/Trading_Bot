# Completed size-feasibility and subsequent-period batch

**No candidate qualifies. The objective of a demonstrated profitable short-term
LONG/SHORT strategy remains unmet.** This batch is finished, not awaiting an
implementation, permission, scheduled window or a local-agent run.

## Actual finding: size feasibility was censoring trades

At BTC prices above100000USDT, minimum0.001BTC cannot fit a100USDT notional
cap. The prior52RSI/56retest rejections were real size conflicts. Following
owner authorization to reconsider isolated research limits, one intervention
was fixed BEFORE its calculation: exactly0.001BTC,notional<=150,1x,risk<=5.
No working setting changed. No alternate cap was swept and no signal,stop,
target,side or indicator was fitted. Earlier failed100-cap records remain.

| BTC2025, fixed0.001BTC | Closed base/stress | Net base USDT | Net stress USDT |
|---|---:|---:|---:|
| RSI15m | 70 / 67 | +28.041636 | +8.581209 |
| Trend-retest adaptation | 94 / 94 | −15.493956 | −28.395407 |

The RSI positive result prompted further validation, not installation. It
still failed the previously registered90%within24h criterion. The decision
to extend economic validation despite that duration failure was documented
before acquiring2026data; the original failed gate was not silently changed.
On separate Jan-Oct2026accounts, identicalRSI yielded63/62closed trades and
**−2.060255/−8.162074USDT** base/stress. No parameters were tuned afterward.

## Completed continuous cross-asset transfer

Frozen RSI rules then tested on the original BTC/ETH/SOL project universe.
Every asset and both execution scenarios reported,not only the best. One
continuous1000USDTaccount per asset/scenario forJan2025-Oct2026,Dec2024warmup;
no annual cash resets. BTCfixed0.001<=150;ETH/SOL<=100notional,1x. No leverage
increase. Size steps/minimums are stated proxies,not historical certification.

| Asset | Closed base/stress | Net base USDT | Net stress USDT | Stress MTM DD | Within24h,stress |
|---|---:|---:|---:|---:|---:|
| BTC | 133 / 129 | +25.981381 | **+0.419136** | 2.204% | 68.99% |
| ETH | 182 / 180 | −2.255960 | **−27.031786** | 5.635% | 90.00% |
| SOL | 146 / 144 | −41.495029 | **−60.560807** | 7.019% | 89.58% |

These are separate accounts,not one combined shared-margin portfolio. All
positions are closed at the end. Closed net allocated by EXIT year:

| Asset | 2025base / stress USDT | 2026base / stress USDT |
|---|---:|---:|
| BTC | +28.041636 / +8.581209 | −2.060255 / −8.162074 |
| ETH | +29.529089 / +16.807994 | −31.785049 / −43.839780 |
| SOL | −14.264316 / −33.826892 | −27.230712 / −26.733915 |

Year subtotals group entire closed-trade economics by exit time; they are
not independently funded annual simulations or full annual MTM returns.
The BTC2025/2026separate runs happen to reconcile with continuous outcomes
because there is no year-end position and fixed quantity; continuous peak-to-
trough drawdown is nevertheless larger than separate annual drawdowns.

BTC stress without its best trade is−1.970371USDT. Its0.419136positive total
over22months does not establish robust advantage or coverage of VM/API/development
costs, which are NOT deducted in these trading PnLs. It fails the positive-
each-year,best-trade,2%DD and duration gates. ETH/SOL fail economic gates.
The dataset was reused and source rules selected after publication; these are
historical robustness checks,not independently verified alpha. No hypothesis
that all possible strategies are unprofitable follows from this result.

## Execution and source evidence

15m closed signals,original asymmetric RSI thresholds,EMA/ATR/body filters,
stateful pullback,source macro windows and cooldown remain fixed. Immediate
protection differs from Pine's initial bracket scheduling. Both scenarios use
5bp fee per side;impact(including spread)5bp base/10bp stress and1/6minute
entry delay. Exits costed taker,adverse stop before target on ambiguous minute;
actual funding events retained. These are scenarios,not measured queue/fills.

Original RSI source3216d14;size intervention preregistrationebe9bd3;
2026economic continuation2fbe6cc;initial33archive receipt420458b;
verifieddaily mark supplementa14e227;crossasset protocolb680a95;
210source receipts and gap diagnosisd5f5942. See corresponding protocol files.

All210continuous-input archives were verified against officialSHA256: monthly
trade1m,mark1m,funding for3assets,Dec2024-Oct2026,plus3officialdaily mark files
for2026-06-29. Monthly archives omitted that day's mark records. The gap was
diagnosed before any2026result; the first input validation aborted before a
model result. Official checksum-verified daily archives supplied observed
prices; no interpolation or invented return. No contradictory overlap accepted.
All3assets have2100funding events and complete trade/mark coverage after the
supplements. Exact source URLs/checksums and gap receipts are saved.

## Verification, artifacts and operational state

Independent `tools/audit_size_and_crossasset.py` audits reference-price gross,
both-side fill fees,impact formula,funding sums and event bounds,signal delay,
no overlapping positions,quantity steps,notional/risk and summary totals.
**914closed records** in the continuous cross-asset runs pass. A further450
size/period diagnostic records pass,but overlap BTCeconomic observations and
must not be advertised as450additional independent market observations.
Future-truncation checks pass; cash reconciles,no unresolved/open position.
Per-asset pass/fail gates and exit-year breakdowns are machine-readable.

Artifacts:
- research/size_feasibility_results: four2025size diagnostic cells.
- research/rsi_size_2026_results: fixed subsequent-period validation.
- research/rsi_crossasset_results: sixcontinuouscells,completejournals,
  source_manifest,independent_audit,gates and gap_diagnosis.

Reproduction uses the registered scripts and public archives,then:
```
python -B tools/audit_size_and_crossasset.py
```

No full application suite: only isolated research changed. No production
code,VM,workingaccount,pause,Telegram,observer,key,budget,credential or paid
schedule accessed or modified. No paidAPI requests,new service,collector,
local-agent task,real/Testnet order or forced trade. No background AI task
is left running. No deployment candidate or profitable-bot completion is claimed.
