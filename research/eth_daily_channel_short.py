"""Rebuild the ETH channel account with SHORT entries only."""
import argparse
import gzip
import json
from pathlib import Path
from unittest.mock import patch

from research import low_turnover_screen as screen

SELECTION = {"symbol": "ETHUSDT", "rule": "channel_20"}
RESEARCH = Path(__file__).parent
CASE = "ETHUSDT:channel_20"


def evaluate(archive, output, stage):
    output.mkdir(parents=True, exist_ok=True)
    return screen.evaluate(archive, output, stage, SELECTION, max_notional=50, direction="SHORT")[CASE]


def attribution(prior, actual):
    def read(path):
        rows = json.loads(gzip.decompress(path.read_bytes()))
        return {row["signal_ms"]: row["net"] for row in rows
                if row["direction"] == "SHORT" and "net" in row}

    old, new = read(prior), read(actual)
    shared = old.keys() & new.keys()
    if any(abs(old[t] - new[t]) > 1e-8 for t in shared):
        raise ValueError("Execution changed for a shared signal")
    extra = new.keys() - old.keys()
    displaced = old.keys() - new.keys()
    return dict(shared=len(shared), extra=len(extra), displaced=len(displaced),
                extra_net=sum(new[t] for t in extra), displaced_net=sum(old[t] for t in displaced),
                profitable=sum(net > 0 for net in new.values()), prior_net=sum(old.values()))


def run(earlier, recent, output):
    core = screen.core
    start = core.stamp("2023-12-01")
    end = core.stamp("2025-01-01")
    with patch.multiple(core, START=start, BEGIN=core.stamp("2024-01-01"), END=end,
                        N=(end - start) // core.M), patch.object(screen, "SPLIT", end):
        periods = {"2024": evaluate(earlier, output / "2024", "development")}
    periods["2025"] = evaluate(recent, output / "2025", "development")
    periods["2026"] = evaluate(recent, output / "2026", "validation")
    for year, record in periods.items():
        stage = "validation" if year == "2026" else "development"
        before = RESEARCH / ("eth_daily_channel_2024_results" if year == "2024"
                             else "eth_daily_channel_50_results")
        for scenario, case in record["cases"].items():
            filename = f"{stage}-ETHUSDT-channel_20-{scenario}.json.gz"
            case["attribution"] = attribution(before / filename, output / year / filename)
        # The prior screen's two-direction gate is inapplicable to this diagnostic.
        record.pop("passes")
    cases = [case for result in periods.values() for case in result["cases"].values()]
    stress = [result["cases"]["stress"] for result in periods.values()]
    ready = (all(case["net"] > 0 and case["max_drawdown"] <= .02 and not case["unresolved"]
                 for case in cases) and all(case["without_best_trade"] > 0 for case in stress)
             and sum(case["closed"] for case in stress) >= 20)
    report = dict(periods=periods, qualifies_for_new_data=ready)
    (output / "summary.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("earlier", "recent", "output"):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    run(args.earlier, args.recent, args.output)
