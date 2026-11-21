"""Run the unchanged short-only ETH channel on earlier years."""
import argparse
import json
from pathlib import Path
from unittest.mock import patch

from research import eth_daily_channel_short as short

core = short.screen.core


def run(archives, output):
    output.mkdir(parents=True, exist_ok=True)
    periods = {}
    for year in (2022, 2023):
        archive = archives / str(year)
        manifest = json.loads((archive / "extreme-restore-manifest.json").read_text())
        if manifest["unavailable"]:
            periods[year] = dict(unavailable=manifest["unavailable"])
            continue
        start = core.stamp(f"{year - 1}-12-01")
        begin = core.stamp(f"{year}-01-01")
        end = core.stamp(f"{year + 1}-01-01")
        with patch.multiple(core, START=start, BEGIN=begin, END=end, N=(end - start) // core.M), \
             patch.object(short.screen, "SPLIT", end):
            result = short.evaluate(archive, output / str(year), "development")
        result.pop("passes")
        periods[year] = result
    cases = [case for period in periods.values() for case in period.get("cases", {}).values()]
    stress = [period["cases"]["stress"] for period in periods.values() if "cases" in period]
    passes = (len(cases) == 4 and all(case["net"] > 0 and case["max_drawdown"] <= .02
                                     and not case["unresolved"] for case in cases)
              and all(case["without_best_trade"] > 0 for case in stress)
              and sum(case["closed"] for case in stress) >= 20)
    (output / "summary.json").write_text(
        json.dumps(dict(periods=periods, passes=passes), indent=2, allow_nan=False) + "\n",
        encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.archives, args.output)
