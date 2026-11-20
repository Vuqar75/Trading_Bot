"""Offline replay of the frozen BTC-to-ETH hourly delay hypothesis."""
import argparse
import gzip
import json
import math
from pathlib import Path

import numpy as np

from research import extreme_reversal as engine

HOUR = 60 * engine.M
SPLIT = engine.stamp("2026-01-01")


def closed_returns(minutes):
    hours = minutes.reshape(-1, 60, 4)
    complete = np.isfinite(hours).all(axis=(1, 2))
    closes = np.where(complete, hours[:, -1, 3], np.nan)
    return closes[1:] / closes[:-1] - 1


def signals_from(btc, eth, start, begin, end):
    leader, follower = closed_returns(btc), closed_returns(eth)
    if leader.shape != follower.shape:
        raise ValueError("unaligned inputs")
    signals = []
    valid_hours = invalid_hours = 0
    for i, (rb, re) in enumerate(zip(leader, follower)):
        boundary = start + (i + 2) * HOUR
        if not begin <= boundary < end:
            continue
        if not (math.isfinite(rb) and math.isfinite(re)):
            invalid_hours += 1
            continue
        valid_hours += 1
        if abs(rb) >= 0.01 and 0 <= re / rb < 0.5:
            signals.append(dict(signal_ms=boundary, direction="LONG" if rb > 0 else "SHORT",
                                btc_return=float(rb), eth_return=float(re)))
    return signals, valid_hours, invalid_hours


def replay(signals, trade, mark, funding, stress):
    account = dict(cash=1000., peak=1000., max_dd=0.)
    busy_until = 0
    unresolved = False
    records = []
    delay = 6 if stress else 1
    for signal in signals:
        t = signal["signal_ms"]
        if unresolved:
            result = dict(rejection="previous_position_unresolved")
        elif t < busy_until:
            result = dict(ignored="position_open")
        elif t + (delay + 60) * engine.M >= engine.END:
            result = dict(rejection="incomplete_final_horizon")
        else:
            result = engine.execute(signal, trade, mark, funding, account, "ETHUSDT",
                                    stress, hold_minutes=60)
        busy_until = result.get("exit_ms", busy_until)
        unresolved |= "unresolved" in result
        records.append({**signal, **result})
    overall = engine.summarize(records, account)
    if not unresolved and not math.isclose(1000 + overall["net"], account["cash"], abs_tol=1e-8):
        raise ValueError("ledger mismatch")
    summary = {"all": overall, "periods": {}}
    for period, start, end in (("2025", engine.BEGIN, SPLIT), ("2026-01_10", SPLIT, engine.END)):
        rows = [r for r in records if start <= r["signal_ms"] < end]
        groups = {"both": rows, **{s: [r for r in rows if r["direction"] == s]
                                   for s in ("LONG", "SHORT")}}
        summary["periods"][period] = {
            key: {**engine.summarize(group, dict(cash=None, max_dd=None)), "signals": len(group)}
            for key, group in groups.items()}
    return summary, records


def decision(scenarios):
    gates = {}
    for scenario, summary in scenarios.items():
        gates[scenario + "/drawdown"] = summary["all"]["max_drawdown"] <= 0.02
        gates[scenario + "/resolved"] = not summary["all"]["unresolved"]
        for period, groups in summary["periods"].items():
            prefix = f"{scenario}/{period}/"
            gates[prefix + "count"] = groups["both"]["closed"] >= 30
            gates[prefix + "sides"] = all(groups[s]["closed"] >= 10 for s in ("LONG", "SHORT"))
            gates[prefix + "net"] = groups["both"]["net"] > 0
            if scenario == "stress":
                gates[prefix + "without_best"] = groups["both"]["without_best_trade"] > 0
    return gates


def run(root, out):
    provenance = {}
    def read(symbol):
        trade, mark, fund, files, overlap = engine.load_data(root, symbol)
        provenance[symbol] = dict(files=files, archive_rest_differences=overlap,
                                  missing_trade_minutes=int(np.isnan(trade[:, 0]).sum()),
                                  missing_mark_minutes=int(np.isnan(mark[:, 0]).sum()))
        return trade, mark, fund

    btc = read("BTCUSDT")[0]
    eth, mark, fund = read("ETHUSDT")
    signals, valid, missing = signals_from(btc, eth, engine.START, engine.BEGIN, engine.END)
    funding = {}
    for t, rate in fund:
        funding.setdefault(engine.ix(t - t % engine.M), []).append((t, rate))
    out.mkdir(parents=True, exist_ok=True)
    scenarios = {}
    for scenario in ("base", "stress"):
        scenarios[scenario], journal = replay(signals, eth, mark, funding, scenario == "stress")
        payload = json.dumps(journal, separators=(",", ":"), allow_nan=False).encode()
        (out / f"{scenario}.json.gz").write_bytes(gzip.compress(payload, mtime=0))
    gates = decision(scenarios)
    result = dict(accepted=all(gates.values()), criteria=gates, valid_hours=valid,
                  invalid_hours=missing, signals=len(signals), scenarios=scenarios)
    for name, data in (("summary", result), ("inputs", provenance)):
        (out / f"{name}.json").write_text(json.dumps(data, indent=2, allow_nan=False) + "\n",
                                          encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive_root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.archive_root, args.output)
