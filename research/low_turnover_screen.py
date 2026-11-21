"""Fixed offline daily strategy screen and frozen chronological validation."""
import argparse
from collections import defaultdict
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np

from research import extreme_reversal as core
from research.flow_weekend import metrics
from research.mark_dislocation import SPLIT, simulate

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
RULES = ("momentum_7", "momentum_7_30", "channel_20", "shock_reversal")
HOLD = 72 * 60
DAY_MIN = 1440
DAY = DAY_MIN * core.M


def daily_signals(trade, begin, end):
    days = trade.reshape(-1, DAY_MIN, 4)
    high = days[:, :, 1].max(axis=1)
    low = days[:, :, 2].min(axis=1)
    close = days[:, -1, 3]
    valid = np.isfinite(days).all(axis=(1, 2))
    signals = {rule: [] for rule in RULES}
    for time in range(begin, end, DAY):
        i = (time - core.START) // DAY - 1
        if not valid[i]:
            continue
        now = close[i]
        momentum = np.sign(now - close[i - 7]) if valid[i - 7] else 0
        slow = np.sign(now - close[i - 30]) if valid[i - 30] else 0
        breakout = 0
        if valid[i - 20:i].all():
            breakout = 1 if now > high[i - 20:i].max() else -1 if now < low[i - 20:i].min() else 0
        reversal = 0
        if valid[i - 1]:
            change = now / close[i - 1] - 1
            if abs(change) >= .02 - 1e-12:
                reversal = -np.sign(change)
        decisions = (momentum, momentum if momentum == slow else 0, breakout, reversal)
        for rule, side in zip(RULES, decisions):
            if side:
                signals[rule].append(dict(signal_ms=time, direction="LONG" if side > 0 else "SHORT"))
    return signals


def meets_threshold(base, stress):
    return (all(case["closed"] >= 20 and min(v["count"] for v in case["sides"].values()) >= 5
                and case["net"] > 0 and case["max_drawdown"] <= .02 and not case["unresolved"]
                for case in (base, stress)) and stress["without_best_trade"] > 0)


def evaluate(root, output, stage, selected=None, *, max_notional=100):
    summary = {}
    begin, end = (core.BEGIN, SPLIT) if stage == "development" else (SPLIT, core.END)
    symbols = SYMBOLS if selected is None else (selected["symbol"],)
    for symbol in symbols:
        trade, mark, rates, sources, overlaps = core.load_data(root, symbol)
        funding = defaultdict(list)
        for time, rate in rates:
            funding[core.ix(time - time % core.M)].append((time, rate))
        signals = daily_signals(trade, begin, end)
        rules = RULES if selected is None else (selected["rule"],)
        for rule in rules:
            cases = {}
            for name, delay in (("base", 1), ("stress", 6)):
                qualifying = [r for r in signals[rule] if r["signal_ms"] + (HOLD + delay) * core.M < end]
                records, account = simulate(qualifying, trade, mark, funding, symbol, name == "stress",
                                            hold_minutes=HOLD, max_notional=max_notional)
                case = metrics(records, account)
                if not case["unresolved"] and abs(account["cash"] - 1000 - case["net"]) > 1e-8:
                    raise ValueError("Balance mismatch")
                key = f"{stage}-{symbol}-{rule}-{name}.json.gz"
                (output / key).write_bytes(gzip.compress(json.dumps(records, allow_nan=False).encode(), mtime=0))
                cases[name] = case
            summary[f"{symbol}:{rule}"] = dict(cases=cases, sources=sources, overlaps=overlaps,
                                               passes=meets_threshold(cases["base"], cases["stress"]))
            print(stage, symbol, rule, cases["stress"]["net"], flush=True)
    return summary


def run(archive, output, stage, eth_half_size=False):
    output.mkdir(parents=True, exist_ok=True)
    receipt = output / "selection.json"
    previous = output / "development.json"
    fixed = dict(symbol="ETHUSDT", rule="channel_20") if eth_half_size else None
    max_notional = 50 if eth_half_size else 100
    selection = fixed
    if stage == "validation":
        saved = json.loads(receipt.read_text())
        if hashlib.sha256(previous.read_bytes()).hexdigest() != saved["development_sha256"]:
            raise ValueError("Development result changed after selection")
        if saved.get("max_notional", 100) != max_notional:
            raise ValueError("Sizing differs from the frozen development selection")
        selection = saved["selected"]
        if selection is None:
            raise ValueError("No frozen candidate")
    summary = evaluate(archive, output, stage, selection, max_notional=max_notional)
    (output / f"{stage}.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    if stage == "development":
        selection = None
        ready = [key for key in summary if summary[key]["passes"]]
        if ready:
            chosen = min(ready, key=lambda key: (-summary[key]["cases"]["stress"]["net"], key))
            symbol, rule = chosen.split(":")
            selection = dict(symbol=symbol, rule=rule)
        receipt.write_text(json.dumps(dict(selected=selection, candidates=ready, max_notional=max_notional,
                           development_sha256=hashlib.sha256(previous.read_bytes()).hexdigest()),
                           indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("stage", choices=("development", "validation"))
    parser.add_argument("--eth-half-size", action="store_true")
    args = parser.parse_args()
    run(args.archive, args.output, args.stage, args.eth_half_size)
