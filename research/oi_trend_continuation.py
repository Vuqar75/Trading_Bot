"""Offline test of open-interest expansion as a trend continuation filter."""
import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np

from research import extreme_reversal as core
from research.derivative_metrics_data import load as load_metrics
from research.flow_weekend import metrics
from research.mark_dislocation import SPLIT, simulate
from research.oi_response import asof

BEGIN = core.stamp("2025-07-01")
BAR = 240 * core.M
HOLD = 24 * 60


def signals(trade, oi_times, oi_values):
    times = np.arange(BEGIN, core.END, BAR)
    prev, _, start_used = asof(oi_times, oi_values, times - BAR - 15 * core.M)
    latest, _, end_used = asof(oi_times, oi_values, times - 15 * core.M)
    eligible = np.isfinite(prev) & np.isfinite(latest)
    rows = []
    price_missing = 0
    for i, time in enumerate(times):
        close = core.ix(int(time)) - 1
        if not np.isfinite(trade[close - 479:close + 1]).all():
            price_missing += 1
            continue
        if not eligible[i]:
            continue
        change = trade[close, 3] / trade[close - 240, 3] - 1
        if abs(change) < .01 - 1e-12:
            continue
        oi_change = latest[i] / prev[i] - 1
        rows.append(dict(signal_ms=int(time), oi_start_ms=int(start_used[i]),
                         oi_end_ms=int(end_used[i]), change=float(change),
                         oi_change=float(oi_change), oi_pass=bool(oi_change >= .01 - 1e-12),
                         direction="LONG" if change > 0 else "SHORT"))
    return rows, dict(boundaries=len(times), metric_missing=int((~eligible).sum()),
                      price_missing=price_missing)


def individual(rows, trade, mark, funding, symbol, stress):
    outcomes = {"oi_pass": [], "oi_reject": []}
    rejected = Counter()
    delay = 6 if stress else 1
    for row in rows:
        if row["signal_ms"] + (delay + HOLD) * core.M >= core.END:
            rejected["incomplete_final_horizon"] += 1
            continue
        account = dict(cash=1000., peak=1000., max_dd=0.)
        outcome = core.execute(row, trade, mark, funding, account, symbol, stress, hold_minutes=HOLD)
        if "net" not in outcome:
            rejected[outcome.get("rejection", outcome.get("unresolved", "other"))] += 1
            continue
        outcomes["oi_pass" if row["oi_pass"] else "oi_reject"].append(
            dict(row, **outcome, net_bp=outcome["net"] / outcome["entry_notional"] * 10_000)
        )
    summary = {key: dict(n=len(group), positive=sum(r["net_bp"] > 0 for r in group),
                         mean_bp=sum(r["net_bp"] for r in group) / len(group) if group else None)
               for key, group in outcomes.items()}
    return summary, outcomes, rejected


def run(archive, metric_root, output):
    output.mkdir(parents=True, exist_ok=True)
    derivative, quality = load_metrics(metric_root)
    manifest_hash = hashlib.sha256((metric_root / "manifest.json").read_bytes()).hexdigest()
    summary = dict(metrics_manifest_sha256=manifest_hash, assets={})
    for symbol in ("BTCUSDT", "ETHUSDT"):
        trade, mark, rates, sources, overlaps = core.load_data(archive, symbol)
        timestamps, values = derivative[symbol]
        rows, availability = signals(trade, timestamps, values[:, 0])
        filtered = [row for row in rows if row["oi_pass"]]
        funding_by_min = defaultdict(list)
        for time, rate in rates:
            funding_by_min[core.ix(time - time % core.M)].append((time, rate))
        asset = dict(sources=sources, overlaps=overlaps, metrics_quality=quality[symbol],
                     signals=len(rows), selected=len(filtered), availability=availability, scenarios={})
        for scenario, stress in (("base", False), ("stress", True)):
            scenario_summary = {}
            journal = {}
            for policy, selected in (("price_only", rows), ("oi_filter", filtered)):
                records, account = simulate(selected, trade, mark, funding_by_min, symbol, stress,
                                            hold_minutes=HOLD)
                total = metrics(records, account)
                if not total["unresolved"] and abs(account["cash"] - 1000 - total["net"]) > 1e-8:
                    raise ValueError("Balance mismatch")
                periods = {year: metrics([r for r in records if (r["signal_ms"] < SPLIT) == earlier])
                           for year, earlier in (("2025", True), ("2026", False))}
                scenario_summary[policy] = dict(total=total, periods=periods)
                journal[policy] = records
            paired, matched, rejected = individual(rows, trade, mark, funding_by_min, symbol, stress)
            scenario_summary["conditional"] = dict(paired, unavailable=rejected)
            journal["conditional"] = matched
            asset["scenarios"][scenario] = scenario_summary
            (output / f"{symbol}-{scenario}.json.gz").write_bytes(
                gzip.compress(json.dumps(journal, allow_nan=False).encode(), mtime=0)
            )
        summary["assets"][symbol] = asset
        (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive", "metrics", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    run(args.archive, args.metrics, args.output)
