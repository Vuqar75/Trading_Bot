"""Test the frozen regime rule on an earlier ETH sample and on SOL."""
import argparse
import json
from pathlib import Path
from unittest.mock import patch

from research import eth_market_regimes as regimes

core = regimes.core
POLICIES = ("both", "combined")


def run(eth, recent, out):
    out.mkdir(parents=True, exist_ok=True)
    start, begin, end = map(core.stamp, ("2020-12-01", "2021-01-01", "2022-01-01"))
    manifest = json.loads((eth / "extreme-restore-manifest.json").read_text())
    if manifest["unavailable"]:
        earlier = dict(unavailable=manifest["unavailable"])
    else:
        with patch.multiple(core, START=start, BEGIN=begin, END=end, N=(end-start)//core.M):
            earlier = regimes.compare(eth, "ETH-2021", begin, end, out, policies=POLICIES)
    results = {"ETH-2021": earlier}
    for year, begin, end in ((2025, core.BEGIN, regimes.daily.SPLIT),
                             (2026, regimes.daily.SPLIT, core.END)):
        key = f"SOL-{year}"
        results[key] = regimes.compare(recent, key, begin, end, out, symbol="SOLUSDT", policies=POLICIES)
    groups = {"ETH": ("ETH-2021",), "SOL": ("SOL-2025", "SOL-2026")}
    passed = {}
    for asset, keys in groups.items():
        cases = [results[k]["cases"] for k in keys if "cases" in results[k]]
        full = [case[f"combined_{cost}"] for case in cases for cost in ("base", "stress")]
        stress = [case["combined_stress"] for case in cases]
        passed[asset] = (
            len(cases) == len(keys)
            and all(v["net"] > 0 and v["max_drawdown"] <= .02 and not v["unresolved"] for v in full)
            and all(v["without_best_trade"] > 0 for v in stress)
            and sum(v["closed"] for v in stress) >= 20
        )
    payload = dict(periods=results, passes=passed)
    (out / "summary.json").write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n",
                                     encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("eth", "recent", "output"):
        parser.add_argument(field, type=Path)
    args = parser.parse_args()
    run(args.eth, args.recent, args.output)
