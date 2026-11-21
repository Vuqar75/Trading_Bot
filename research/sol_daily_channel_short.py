"""Replay the frozen ETH short-channel rule on SOL."""
import argparse
import json
from pathlib import Path

from research import low_turnover_screen as screen

SELECTION = {"symbol": "SOLUSDT", "rule": "channel_20"}
KEY = "SOLUSDT:channel_20"


def run(archive, out):
    out.mkdir(parents=True, exist_ok=True)
    data = {
        period: screen.evaluate(archive, out, stage, SELECTION, max_notional=50, direction="SHORT")[KEY]
        for period, stage in (("2025", "development"), ("2026", "validation"))
    }
    for period in data.values():
        period.pop("passes")
    cases = [case for period in data.values() for case in period["cases"].values()]
    stress = [period["cases"]["stress"] for period in data.values()]
    passes = (all(case["net"] > 0 and case["max_drawdown"] <= .02 and not case["unresolved"]
                  for case in cases)
              and all(case["without_best_trade"] > 0 for case in stress)
              and sum(case["closed"] for case in stress) >= 20)
    (out / "summary.json").write_text(
        json.dumps(dict(periods=data, passes=passes), indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.archive, args.output)
