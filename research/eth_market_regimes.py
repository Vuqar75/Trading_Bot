"""Compare ETH long, short, range and a simple regime switch offline."""
import argparse
from collections import defaultdict
import gzip
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np

from research import low_turnover_screen as daily

core = daily.core
REGIMES = ("up", "down", "range")
POLICIES = ("long", "short", "both", "range", "combined")


def signals(trade, begin, end):
    days = trade.reshape(-1, daily.DAY_MIN, 4)
    high, low, close = days[:, :, 1].max(axis=1), days[:, :, 2].min(axis=1), days[:, -1, 3]
    valid = np.isfinite(days).all(axis=(1, 2))
    choices = {name: [] for name in POLICIES}
    for time in range(begin, end, daily.DAY):
        i = (time - core.START) // daily.DAY - 1
        if i < 90 or not valid[i-90] or not valid[i-20:i+1].all():
            continue
        value = close[i]
        return_90d = float(value / close[i-90] - 1)
        regime = ("up" if return_90d >= .05 - 1e-12 else
                  "down" if return_90d <= -.05 + 1e-12 else "range")
        upper, lower = high[i-20:i].max(), low[i-20:i].min()
        row = dict(signal_ms=time, regime=regime, return_90d=return_90d)
        if value > upper or value < lower:
            direction = "LONG" if value > upper else "SHORT"
            trend = dict(row, direction=direction, signal_type="breakout")
            choices["both"].append(trend)
            choices[direction.lower()].append(trend)
            if (regime, direction) in (("up", "LONG"), ("down", "SHORT")):
                choices["combined"].append(trend)
        elif regime == "range" and upper > lower:
            location = (value - lower) / (upper - lower)
            if location <= .1 + 1e-12 or location >= .9 - 1e-12:
                direction = "LONG" if location <= .1 + 1e-12 else "SHORT"
                fade = dict(row, direction=direction, signal_type="fade")
                choices["range"].append(fade)
                choices["combined"].append(fade)
    return choices


def compare(root, year, begin, end, out, *, symbol="ETHUSDT", policies=POLICIES):
    trade, mark, rates, files, overlaps = core.load_data(root, symbol)
    funding = defaultdict(list)
    for t, rate in rates:
        funding[core.ix(t - t % core.M)].append((t, rate))
    choices = signals(trade, begin, end)
    cases = {}
    for policy in policies:
        raw = choices[policy]
        for scenario, delay in (("base", 1), ("stress", 6)):
            allowed = [row for row in raw if row["signal_ms"] + (daily.HOLD + delay) * core.M < end]
            rows, account = daily.simulate(allowed, trade, mark, funding, symbol,
                                           scenario == "stress", hold_minutes=daily.HOLD, max_notional=50)
            total = daily.metrics(rows, account)
            if not total["unresolved"] and abs(total["net"] + 1000 - account["cash"]) > 1e-8:
                raise ValueError("Balance mismatch")
            total["regimes"] = {key: daily.metrics([r for r in rows if r["regime"] == key])
                                for key in REGIMES}
            key = f"{policy}_{scenario}"
            cases[key] = total
            (out / f"{year}-{key}.json.gz").write_bytes(
                gzip.compress(json.dumps(rows, allow_nan=False).encode(), mtime=0)
            )
    print(year, {k: round(v["net"], 2) for k, v in cases.items() if k.endswith("_stress")}, flush=True)
    return dict(cases=cases, sources=files, overlaps=overlaps)


def run(early, earlier, recent, out):
    out.mkdir(parents=True, exist_ok=True)
    results = {}
    for year, archive in ((2022, early / "2022"), (2023, early / "2023"), (2024, earlier)):
        start = core.stamp(f"{year-1}-12-01")
        begin = core.stamp(f"{year}-01-01")
        end = core.stamp(f"{year+1}-01-01")
        with patch.multiple(core, START=start, BEGIN=begin, END=end, N=(end-start)//core.M):
            results[year] = compare(archive, year, begin, end, out)
    for year, begin, end in ((2025, core.BEGIN, daily.SPLIT), (2026, daily.SPLIT, core.END)):
        results[year] = compare(recent, year, begin, end, out)
    aggregate = {}
    for policy in POLICIES:
        by_scenario = {}
        for scenario in ("base", "stress"):
            cases = [result["cases"][f"{policy}_{scenario}"] for result in results.values()]
            by_scenario[scenario] = dict(
                n=sum(v["closed"] for v in cases), net=sum(v["net"] for v in cases),
                max_drawdown=max(v["max_drawdown"] for v in cases),
                positive_periods=sum(v["net"] > 0 for v in cases),
                all_periods_positive_without_best=all(v["without_best_trade"] > 0 for v in cases),
                unresolved=sum(sum(v["unresolved"].values()) for v in cases),
                regimes={regime: dict(n=sum(v["regimes"][regime]["closed"] for v in cases),
                                      net=sum(v["regimes"][regime]["net"] for v in cases))
                         for regime in REGIMES},
            )
        passes = (all(v["positive_periods"] == len(results) and v["max_drawdown"] <= .02
                      and not v["unresolved"] for v in by_scenario.values())
                  and by_scenario["stress"]["all_periods_positive_without_best"]
                  and by_scenario["stress"]["n"] >= 20)
        aggregate[policy] = dict(by_scenario, passes=passes)
    (out / "summary.json").write_text(json.dumps(dict(periods=results, aggregate=aggregate),
                                                 indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("early", "earlier", "recent", "output"):
        parser.add_argument(key, type=Path)
    args = parser.parse_args()
    run(args.early, args.earlier, args.recent, args.output)
