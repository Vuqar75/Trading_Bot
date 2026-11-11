# Additional source checks after actual retest calculation

No foreign source was imported or run. Per-file hashes and pinned commits are
in research/external_strategy_review/leadlag_scalp_receipts.json. These checks
avoid commissioning backtests that cannot establish the claimed results.

1. https://github.com/mateofrqt/Crypto-LeadLag-Strategy at
1cf5fd177b518508ad71371fdd5d65804e78a827:
README explicitly withholds current signal construction, thresholds and
parameters. Public code is declared outdated. The15m model reports58 trades
and PF1.89 but its9-trade OOS is flat-to-negative. The1m model reports31OOS
trades but mentions Optuna evaluated on a held-out window, so search/validation
separation needs clarification. Positive daily123-asset results are not the
15m strategy. Without current rules and a ledger, do not reproduce from guessed
thresholds or label an approximation its verified model. No backtest run.

2. https://github.com/kiattisakph/crypto-scalp-bot at
0ed020964abecedb6c587a7bdb2897b1ae360991:
README's23.46% gain is explicitly a Console Report Example, not a committed
verified trade dataset. No trackedCSV/JSON/parquet ledger was found.
backtest/data_fetcher.py::_kline_to_dict assigns timestamp from kline[0], i.e.
OPEN time. backtest_engine.py slices3m and15m full candles bytimestamp<=ts,
then evaluates and opens at the current3m close stampedts. At a3m timestamp
within an unclosed15m bar this exposes that bar's final data before availability.
The saved-position PnL formula is raw price difference times quantity; the
inspected backtest engine has no fee/funding/slippage charge. Declared live
funding/spread filters do not provide that missing historical accounting.
This is sufficient to reject the public backtest as evidence of net scalping
profit; it is not a proof every possible corrected version loses money.
No API credentials, bot entrypoint or downloaded trading code was executed.

Operational state unchanged. Overall profitable-bot objective is unfinished.
