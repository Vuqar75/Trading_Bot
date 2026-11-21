"""Execution-cost sensitivity on immutable RSI fill journals."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path

RSI_SOURCES = {
    "2025": "size_feasibility_results/rsi/trades.json.gz",
    "2026": "rsi_size_2026_results/trades.json.gz",
}
FEE = .0005


def evaluate(rows):
    fees_only = []
    turnover = 0.
    net = 0.
    for row in rows:
        recorded_net = (row["gross"] + row["funding"] - row["fees"]
                        - row["impact"] - row["liquidation_fee"])
        if abs(recorded_net - row["net"]) > 1e-8:
            raise ValueError("Invalid archived PnL")
        raw_turnover = row["qty"] * (row["entry_reference"] + row["exit_reference"])
        turnover += raw_turnover
        net += recorded_net
        fees_only.append(row["gross"] + row["funding"] - raw_turnover * FEE - row["liquidation_fee"])
    best = max(fees_only, default=0)
    optimistic = sum(fees_only)
    return dict(
        n=len(rows), turnover=turnover, archived_net=net, zero_impact_net=optimistic,
        zero_impact_without_best=optimistic-best,
        break_even_per_leg_bp=optimistic/turnover*10_000 if turnover else None,
        fraction_closed_24h=sum(row["hold_hours"] <= 24 for row in rows)/len(rows) if rows else None,
        entries_over_100=sum(row["entry"] * row["qty"] > 100 for row in rows)
    )


def run(root, out):
    periods = {}
    for period, path in RSI_SOURCES.items():
        raw = (root / path).read_bytes()
        records = json.loads(gzip.decompress(raw))
        periods[period] = dict(source=path, sha256=hashlib.sha256(raw).hexdigest(),
                               cases={key: evaluate(rows) for key, rows in records.items()})
    out.write_text(json.dumps(periods, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("research_root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.research_root, args.output)
