"""Offline source inventory and saved-CSV checks; no strategy execution."""
import argparse
import csv
import hashlib
import json
import subprocess
from pathlib import Path

SOURCES = {
    "rsi": ("3216d14f111d705bd48b3b1746bd7cbaa2832653", ["README.md", "strategies/version20_pullback_regimeTP.pine", "strategies/version21_hiwr.pine"]),
    "retest": ("f750f2ba12a83ace5de0f2e6d056bfcdaa6fe171", ["README.md", "main.py", "oos_validation.py", "data_loader.py", "indicators.py", "results/oos_validation/oos_results.csv"]),
    "positioning": ("e6d26a6d41693ea74024cf699942f72f224e296c", ["strategies/positioning-based-btc.md", "strategies/honest-backtest-2026-04-14.md", "strategies/barrel-oos-honest-2026-04-15.md"]),
}


def review(paths):
    result = {}
    for name, (pin, files) in SOURCES.items():
        root = Path(paths[name])
        revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        if revision != pin:
            raise ValueError(f"Unexpected {name} revision: {revision}")
        tracked = subprocess.check_output(["git", "-C", str(root), "ls-files"], text=True).splitlines()
        result[name] = {"commit": revision, "tracked_files": len(tracked),
                        "file_sha256": {f: hashlib.sha256((root / f).read_bytes()).hexdigest() for f in files}}
        if name in ("rsi", "positioning"):
            extensions = {".csv", ".json", ".jsonl", ".py", ".npz", ".parquet", ".js", ".mjs", ".ts"}
            result[name]["tracked_code_or_ledger_files_except_pine"] = [f for f in tracked if Path(f).suffix in extensions]
        if name == "retest":
            with (root / files[-1]).open() as stream:
                rows = list(csv.DictReader(stream))
            assert len({r["Symbol"] for r in rows}) == len(rows)
            result[name]["oos_csv_profile"] = {
                "rows": len(rows),
                "mean_profit_factor": sum(float(r["Profit Factor"]) for r in rows) / len(rows),
                "positive_returns": sum(float(r["Return (%)"]) > 0 for r in rows),
                "pf_at_least_1_5": sum(float(r["Profit Factor"]) >= 1.5 for r in rows),
            }
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in SOURCES:
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(review({name: getattr(args, name) for name in SOURCES}), indent=2) + "\n")
