"""Replay the frozen ETH channel rule on a separate earlier archive."""
import argparse
import json
from pathlib import Path
from unittest.mock import patch

from research import low_turnover_screen as screen


def run(archive, output):
    core = screen.core
    start = core.stamp("2023-12-01")
    begin = core.stamp("2024-01-01")
    end = core.stamp("2025-01-01")
    selection = {"symbol": "ETHUSDT", "rule": "channel_20"}
    output.mkdir(parents=True, exist_ok=True)
    with patch.multiple(core, START=start, BEGIN=begin, END=end, N=(end - start) // core.M), \
         patch.object(screen, "SPLIT", end):
        results = screen.evaluate(archive, output, "development", selection, max_notional=50)
    report = dict(period="2024", frozen_commit="650b460", max_notional=50, results=results)
    (output / "summary.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n",
                                         encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.archive, args.output)
