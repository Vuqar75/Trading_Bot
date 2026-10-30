# Paper futures research, stage 1

This branch is an OFFLINE research implementation. It does not change the live
Cloud Shell checkout, its database, Telegram configuration, or active process.
No candidate strategy is exposed in the normal bot launcher or Telegram commands.

## Reproduce

Python 3.12+, standard library only. Run from this isolated research checkout:

```bash
python3 -B -m unittest discover -s tests -v
python3 -B tools/fetch_candles.py --start 2026-09-30 --end 2026-10-30 \
  --output research_data/btc-testnet-1m.csv
python3 -B tools/verify_legacy.py --candles research_data/btc-testnet-1m.csv \
  --split-ms 1792454400000
python3 -B -m trading_bot.replay --candles research_data/btc-testnet-1m.csv \
  --split 2026-10-20 --output research_results/comparison.json
```

Fetch and replay are separate. Replay imports no launcher or Telegram module,
does not load environment configuration, has no database path option, and uses
only an in-memory SQLite database and a fake exchange. AI is explicitly disabled.
Fetch uses only the public Spot Testnet endpoint. A later fetch may differ after
a Testnet reset: retain the original CSV and its SHA-256 provenance manifest.
The Linux tests use temporary directories/databases, not the bot's data directory.
Two pre-existing tests are POSIX-dependent (chmod permissions and SQLite cleanup);
Cloud Shell/Linux is the authoritative full-suite environment.

## Model and no-lookahead contract

- Fixed BTCUSDT, paper futures, 10 USDT margin, 1x, 1000 USDT virtual balance.
- Same closed-candle SMA 10/30, 2% price stop, 4% take, UTC daily loss cap 5 USDT.
- At minute t the quote is that minute's OPEN. Only candles closed before t
  enter indicators. Current high/low/close, future candles and future prices
  cannot be used to decide or execute a trade. No interpolation or sorting repairs.
- Stops/liquidation are sampled at minute opens as a polling proxy. Intraminute
  extremes are not treated as known execution opportunities. This cannot reproduce
  the exact original ticker price and polling delay; historical ticker samples
  were not recorded by the live bot.
- Each period starts flat with a fresh balance and at least 31 past candles for
  warmup only. Training force-closes at 2026-10-19 23:59 UTC, strictly BEFORE the
  holdout starts at 2026-10-20 00:00 UTC. Holdout force-closes at the last available
  minute open (2026-10-29 23:59 UTC). Boundary closes pay all costs and are counted.
- Drawdown is peak-to-trough liquidation-value equity at minute opens, including
  unrealized PnL, entry fee and estimated exit fee/slippage, not only closed PnL.
  Intraminute drawdown can be larger. Percentage drawdown uses the running equity peak.
- No funding, orderbook spread, market impact or actual futures quotes are modeled.

## Frozen experiment design

Baseline and each family are compared on identical periods and cost assumptions.
Families: baseline; signed fast-SMA slope filter; confirmation on the next closed
candle; opposite entry after reversal close; all three combined.

Slope thresholds are 0, 0.5, 1 basis points per candle. LONG requires the signed
fast-SMA change to exceed the threshold; SHORT uses the opposite sign. Confirmation
requires SMA regime to persist exactly one additional closed candle. Exits are
never postponed by entry filters. Opposite reentry is considered only on the next
closed candle after a reversal exit, if the opposite regime persists; it is not
an immediate same-tick reversal. In the combined case this next candle also
satisfies confirmation. Risk exits do not automatically request opposite reentry.

Choose one threshold per family using TRAIN net PnL only, tie-break lower drawdown,
then lower threshold. Freeze family choices and the overall training winner before
running any holdout. Report all frozen families on the holdout, without reselecting
or changing thresholds from those results. No minimum trade count was fitted.
The three known October 30 trades only test arithmetic, not strategy selection.

Default comparison uses 10 bps entry + 10 bps exit and zero slippage (legacy
baseline). The separately labeled stress case uses 2 bps adverse slippage per
side, with frozen parameters and unchanged fees. Replay flags:
`--entry-fee-bps`, `--exit-fee-bps`, `--slippage-bps`, `--stress-slippage-bps`.

## Costs and journal

Normal code defaults remain 10/10/0 bps. `PAPER_ENTRY_FEE_BPS`,
`PAPER_EXIT_FEE_BPS`, `PAPER_SLIPPAGE_BPS` configure PAPER FUTURES ONLY;
spot/testnet execution accounting is unchanged. 1 bp = 0.01%. Values must be
finite, nonnegative and below 10000. No new strategy is enabled by these settings.
Entry sizing remains margin * leverage / adverse entry fill, preserving nominal.
Opening LONG and closing SHORT buy higher; opening SHORT and closing LONG sell lower.

`execution_journal` is additive: it is created only when this version opens a Store.
Nothing migrates the active bot database during this research task. Futures events,
position/balance, journal and notification are committed in the same transaction.
Each OPEN/CLOSE pair shares a position_id. Records include quote/fill, quantity,
entry/exit fees, executed-price gross and net PnL, event slippage cost, isolated-loss
cap adjustment, timestamps/holding time, reason, candle/SMA snapshot, costs and
strategy parameters. Only allowlisted strategy fields are saved, never credentials.
Pre-existing positions close with `legacy_position=1`; unknown entry time/cost
configuration is explicitly null, not fabricated. Old trade rows are not backfilled.

For aggregation use CLOSE rows for gross/net PnL and entry_fee + exit_fee (entry_fee
is repeated on CLOSE for self-contained reconciliation). Sum slippage_cost across
OPEN and CLOSE. Gross already reflects slippage; do NOT subtract slippage twice:

    net = gross_at_fills - entry_fee - exit_fee + loss_cap_adjustment
    gross_before_slippage = gross_at_fills + entry_slippage + exit_slippage

The second formula uses the same filled quantity, not a separate zero-slip strategy
run. Position sizing, stop triggers and paths can change under different costs.
The isolated loss cap means a simulated extreme gap cannot lose more than margin.

Commission share is fees / abs(net loss) for net-negative periods, and null for
nonnegative net. It can exceed 100% when gross gains offset fees. This is not a
causal attribution of all losses. A separate fees/(fees+slippage) ratio is included.

## Validation and limits

`tools/verify_legacy.py` loads the unchanged core from Git revision
`443ca56bacaf225c52ecef1a504a38e25672b8fc` into a separate module and compares every
baseline event with the new replay (timestamps, direction, quantity, fill, net, reason).
The raw baseline code is not modified. Tests also perturb future candles and the
entire holdout to prove past results and training choices are unchanged.

This dataset starts on October 7, not the requested September 30: missing initial
coverage is not filled in. It contains extreme Testnet price moves. The sample
supports implementation verification and a limited period comparison, not a claim
of a profitable, robust strategy or validation for real futures deployment.

## Frozen input for another checkout

The public Testnet snapshot is committed in `research/fixtures/` so a reset of
Testnet does not prevent reproducing stage 1. No private bot database is included.
Restore it from the project root, then use the replay/validation commands above:

```bash
python3 -c "import gzip,pathlib; p=pathlib.Path('research_data'); p.mkdir(exist_ok=True); (p/'btc-testnet-1m.csv').write_bytes(gzip.decompress(pathlib.Path('research/fixtures/btc-testnet-1m.csv.gz').read_bytes()))"
sha256sum research_data/btc-testnet-1m.csv
```

Expected SHA-256: `47d3be14ecdbf872f1b95121c61ca514d799283797d9b1493d14098c3a2cbb00`.
Stage 1 metrics/provenance/legacy validation are committed under `research/`.
Read `HANDOFF.md` before continuing; update it and push an isolated branch after
each completed stage. Deployment to the running bot always needs a separate request.
