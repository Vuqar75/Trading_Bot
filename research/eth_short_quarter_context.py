"""Matched short-only ETH replays with and without a quarter trend gate."""
import argparse
from collections import defaultdict
import gzip
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np

from research import low_turnover_screen as daily

core = daily.core


def compare(root, year, begin, end, out):
    trade, mark, rates, files, overlaps = core.load_data(root, "ETHUSDT")
    funding = defaultdict(list)
    for t, rate in rates:
        funding[core.ix(t - t % core.M)].append((t, rate))
    all_signals = daily.daily_signals(trade, begin, end)["channel_20"]
    signals = []
    for row in all_signals:
        i = core.ix(row["signal_ms"]) - 1
        previous = i - 90 * daily.DAY_MIN
        if row["direction"] != "SHORT" or previous < 0:
            continue
        close, past = trade[[i, previous], 3]
        if np.isfinite(past) and past > 0:
            signals.append(dict(row, negative_quarter=bool(close < past)))
    cases = {}
    for name, delay in (("base", 1), ("stress", 6)):
        complete = [r for r in signals if r["signal_ms"] + (daily.HOLD + delay) * core.M < end]
        for gate, rows in (("control", complete), ("quarter", [r for r in complete if r["negative_quarter"]])):
            journal, account = daily.simulate(rows, trade, mark, funding, "ETHUSDT", name == "stress",
                                              hold_minutes=daily.HOLD, max_notional=50)
            total = daily.metrics(journal, account)
            if not total["unresolved"] and abs(account["cash"] - 1000 - total["net"]) > 1e-8:
                raise ValueError("Unreconciled result")
            key = f"{gate}_{name}"
            cases[key] = total
            (out / f"{year}-{key}.json.gz").write_bytes(
                gzip.compress(json.dumps(journal, allow_nan=False).encode(), mtime=0)
            )
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
    for year, start, end in ((2025, core.BEGIN, daily.SPLIT), (2026, daily.SPLIT, core.END)):
        results[year] = compare(recent, year, start, end, out)
    (out / "summary.json").write_text(json.dumps(results, indent=2, allow_nan=False) + "\n",
                                     encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("early", "earlier", "recent", "output"):
        parser.add_argument(key, type=Path)
    args = parser.parse_args()
    run(args.early, args.earlier, args.recent, args.output)
