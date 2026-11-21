"""Offline daily BTC/ETH/SOL comparison under the same direction signal."""
import argparse
from collections import Counter, defaultdict
import gzip
import json
from pathlib import Path
from statistics import median

import numpy as np

from research import extreme_reversal as core
from research.flow_weekend import metrics
from research.mark_dislocation import SPLIT

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
HOLD = 1440
DAY = HOLD * core.M


def daily_signals(markets):
    signals = []
    missing = 0
    for time in range(core.BEGIN, core.END, DAY):
        i = core.ix(time) - 1
        prices = {s: trade[[i, i - HOLD, i - 7 * HOLD], 3]
                  for s, (trade, _, _) in markets.items()}
        if not all(np.isfinite(row).all() and (row > 0).all() for row in prices.values()):
            missing += 1
            continue
        change = {s: float(close / yesterday - 1)
                  for s, (close, yesterday, _) in prices.items()}
        strength = {s: float(close / last_week - 1)
                    for s, (close, _, last_week) in prices.items()}
        broad = median(change.values())
        if not broad:
            continue
        long = broad > 0
        selected = min(strength, key=lambda s: (-strength[s] if long else strength[s], s))
        signals.append(dict(signal_ms=time, direction="LONG" if long else "SHORT",
                            median_1d=broad, returns_1d=change, returns_7d=strength,
                            selected=selected))
    return signals, missing


def position(signal, markets, policy, account, stress):
    symbol = signal["selected"] if policy == "rotation" else "BTCUSDT"
    trade, mark, funding = markets[symbol]
    outcome = core.execute(signal, trade, mark, funding, account, symbol, stress, hold_minutes=HOLD)
    return dict(signal, symbol=symbol, **outcome)


def replay(signals, markets, policy, stress):
    account = dict(cash=1000., peak=1000., max_dd=0.)
    busy_through = -1
    halted = False
    rows = []
    delay = (6 if stress else 1) * core.M
    for signal in signals:
        t = signal["signal_ms"]
        if halted:
            row = dict(signal, rejection="previous_position_unresolved")
        elif t <= busy_through:
            row = dict(signal, ignored="position_or_pending")
        elif t + delay + DAY >= core.END:
            row = dict(signal, rejection="incomplete_final_horizon")
        else:
            row = position(signal, markets, policy, account, stress)
            halted = "unresolved" in row
            busy_through = row.get("exit_ms", t + delay)
        rows.append(row)
    return rows, account


def paired_signals(signals, markets, stress):
    delay = (6 if stress else 1) * core.M
    reject = Counter()
    matched = []
    for signal in signals:
        if signal["signal_ms"] + delay + DAY >= core.END:
            continue
        outcomes = [position(signal, markets, policy, dict(cash=1000., peak=1000., max_dd=0.),
                             stress) for policy in ("rotation", "btc")]
        if any("net" not in row for row in outcomes):
            reject.update(row.get("rejection", row.get("unresolved", "other")) for row in outcomes
                          if "net" not in row)
            continue
        rotation, btc = (row["net"] / row["entry_notional"] * 10_000 for row in outcomes)
        matched.append(dict(time=signal["signal_ms"], selected=signal["selected"],
                            direction=signal["direction"], rotation_bp=rotation, btc_bp=btc))
    return matched, reject


def run(root, out):
    out.mkdir(parents=True, exist_ok=True)
    markets = {}
    sources = {}
    for symbol in SYMBOLS:
        trade, mark, funding, files, overlap = core.load_data(root, symbol)
        by_minute = defaultdict(list)
        for time, rate in funding:
            by_minute[core.ix(time - time % core.M)].append((time, rate))
        markets[symbol] = (trade, mark, by_minute)
        sources[symbol] = dict(files=files, overlaps=overlap)
    signals, missing = daily_signals(markets)
    summary = dict(signals=len(signals), missing=missing, sources=sources, cases={}, paired={})
    for name, stress in (("base", False), ("stress", True)):
        for policy in ("rotation", "btc"):
            rows, account = replay(signals, markets, policy, stress)
            result = metrics(rows, account)
            result["by_asset"] = {s: metrics([r for r in rows if r.get("symbol") == s])
                                  for s in SYMBOLS}
            if not result["unresolved"] and abs(account["cash"] - 1000 - result["net"]) > 1e-8:
                raise ValueError("Balance mismatch")
            result["without_best_asset"] = result["net"] - max(
                asset["net"] for asset in result["by_asset"].values()
            )
            result["periods"] = {
                year: metrics([row for row in rows if (row["signal_ms"] < SPLIT) == first])
                for year, first in (("2025", True), ("2026", False))
            }
            key = f"{policy}_{name}"
            summary["cases"][key] = result
            (out / f"{key}.json.gz").write_bytes(gzip.compress(json.dumps(rows, allow_nan=False).encode(), mtime=0))
        matched, rejects = paired_signals(signals, markets, stress)
        n = len(matched)
        summary["paired"][name] = dict(
            n=n, reasons=rejects,
            rotation_mean_bp=sum(r["rotation_bp"] for r in matched) / n if n else None,
            btc_mean_bp=sum(r["btc_bp"] for r in matched) / n if n else None,
        )
        (out / f"paired_{name}.json.gz").write_bytes(
            gzip.compress(json.dumps(matched, allow_nan=False).encode(), mtime=0)
        )
    (out / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.archive, args.output)
