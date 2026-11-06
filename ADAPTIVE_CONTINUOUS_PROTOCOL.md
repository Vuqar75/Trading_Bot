# Adaptive continuous research protocol v1

Fixed before a future start. This is isolated virtual research, not permission
to trade the working paper wallets. All three rules failed economic selection;
their prospective study does not establish profitability.

* Pair BTCUSDT. Policy `isolated-router-policy-v1` and the exact pre-registered
  1h trend/15m range rules in `ADAPTIVE_ROUTER_POLICY.md` and
  `ADAPTIVE_RESEARCH_PLAN.md` remain fixed. TREND_UP uses Spot long, TREND_DOWN
  USD-M isolated short, RANGE Spot long, NO_TRADE blocks entry. Cross/margin
  have no study route. A mode change does not change an open position's rule.
* Two separate experiments (AI and deterministic `sma20-4h-1pct-v1` baseline)
  start at the same actual durable start instant, in cash, each with exactly
  1,000 virtual USDT: 500 Spot and 500 isolated. No automatic transfer after
  initialization. An insufficient market wallet causes a skipped entry; no
  capital is duplicated. Notional at most 10 USDT, isolated leverage 1x,
  at most one position or pending entry per experiment. The two experiments
  never share a financial database or block each other.
* Rules: the 1h trend rule uses 48 previous hourly closes to enter and the
  reverse 24-hour channel to exit, 2% protective threshold, 168h maximum.
  The 15m range uses the previous 96 bars, width <=3%, lower 20% entry and
  midpoint exit, 1% protective threshold, 24h maximum, >=0.6% gross room.
  Neither uses averaging or pyramiding. The signal is based on closed UTC bars,
  and execution is delayed one complete 15m bar. At most five seconds after
  the next boundary, use a current source-timestamped trade for Spot or USD-M
  mark for isolated, with 10 bps fee and 5 bps execution impact each way,
  current exchange filters and downward rounding. No entry is forced to meet a
  minimum. Missing a window is recorded, never executed backwards. Full paper
  fills are assumptions, not proof of fillable liquidity; isolated partial
  close is unsupported. Protective monitoring is periodic and a threshold is
  not a guaranteed fill price or an exchange-held stop.
* A root-owned publisher validates the private executor's contemporaneous
  candle capture, request hash and completed response, then publishes only
  public input and sanitized output into a root-owned study-readable directory.
  The study user cannot read the budget, key or executor-private directory.
  The first eligible decision and signal must both be after actual study start;
  no historic position or signal is inherited. No additional AI request or
  schedule is introduced. When the model is absent or expired, AI entries
  stop while local position servicing continues. The baseline may use its own
  independently collected snapshot after the AI collection window; such a
  snapshot is explicitly unpaired and is not compared as the same input.
* Funding uses BTCUSDT USD-M event rates and marks, saved first-observed time,
  and a previously observed next-due schedule. Source event time is not source
  publication time. Unconfirmed history, an unobserved schedule after a long
  outage or changed/missing/extra already-covered events block new entries;
  no missing rate is set to zero and no old event is silently corrected.
  A fresh price can still trigger protective evaluation; accounting requiring
  unconfirmed funding remains explicitly unavailable.
* Two-account backups capture financial, queue, observations, route and
  market-evidence databases under a cycle lock. A separate uploader can access
  only finished archives, uses the existing private bucket and an isolated
  prefix, then downloads and validates the object in a temporary restore.
  The study process has no metadata-token, budget, key or working-database
  access. No restoration may overwrite a newer history.
* Report realised and open net, costs, funding, exposure, missed/blocked
  checks and data/protection health separately for each account. Unknown
  prices, funding and API charges remain unknown. Known token-derived API cost
  is an estimate, not a confirmed provider bill, and is not double-counted.
  One or a few virtual trades do not establish advantage or profitability.
