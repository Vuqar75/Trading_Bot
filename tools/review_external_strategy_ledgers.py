"""Read public source snapshots as data; never import or execute their bots.

Offline usage: --josiah PATH --rl PATH --sentinel PATH --output receipt.json
Checkout commit IDs are pinned. No HTTP, application DB, keys or orders.
"""
import argparse
import csv
import hashlib
import json
import re
import statistics
import subprocess
from collections import defaultdict
from decimal import Decimal as D
from pathlib import Path

PINS = {
    "josiah": "53067b0218d65da42516259dcbf27d1bb445a9cc",
    "rl": "5df17b4dadc0c0647cead7b310a2b816cbb7e8a0",
    "sentinel": "ca647c099755ae9770f92205d5fc413e8e7c6c95",
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checkout(path, name):
    revision = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    if revision != PINS[name]:
        raise ValueError(f"Unexpected {name} revision: {revision}")
    return revision


def inspect_josiah(path):
    state = json.loads((path / "state/live_state.json").read_text())
    rows = list(csv.DictReader((path / "state/trades.csv").open()))
    assert len(rows) == len({tuple(row.items()) for row in rows})
    result = {"commit": checkout(path, "josiah"), "rows": len(rows),
              "snapshot_utc": state["updated_at"], "books": {}}
    for book in sorted({r["book"] for r in rows}):
        records = [r for r in rows if r["book"] == book]
        pending, completed = {}, []
        for r in records:
            symbol = r["symbol"]
            if r["reason"].startswith("OPEN"):
                assert symbol not in pending, (book, symbol, "duplicate OPEN")
                pending[symbol] = r
            else:
                entry = pending.pop(symbol)
                assert entry["side"] == r["side"] and entry["qty"] == r["qty"]
                side = D(1) if r["side"] == "long" else D(-1)
                net = side * (D(r["exit"]) - D(r["entry"])) * D(r["qty"])
                net -= D(r["fees"]) + D(r["funding"])
                completed.append((net, r))
        snap = state["portfolio"]["state"][book]
        assert set(pending) == set(snap["positions"])
        assert len(completed) == snap["n_trades"]
        margin = sum(D(str(v["margin"])) for v in snap["positions"].values())
        unrealized = sum(D(str(v["side"])) *
                         (D(str(state["last_prices"][symbol])) - D(str(v["entry_price"]))) *
                         D(str(v["qty"])) for symbol, v in snap["positions"].items())
        equity = D(str(snap["cash"])) + margin + unrealized
        cash = (D(str(state["start_usd"]))
                + sum(D(r["pnl"]) for r in records if not r["reason"].startswith("OPEN"))
                - sum(D(r["fees"]) for r in records if r["reason"].startswith("OPEN"))
                - D(str(snap["funding_total"])) - margin)
        residual = cash - D(str(snap["cash"]))
        # Public CSV rounds monetary columns to 4 decimals; snapshot does not.
        tolerance = D("0.0001") * len(records)
        assert abs(residual) <= tolerance, (book, residual, tolerance)
        by_side, by_module = defaultdict(D), defaultdict(D)
        for net, r in completed:
            by_side[r["side"]] += net
            by_module[r["module"]] += net
        opens = [r for r in records if r["reason"].startswith("OPEN")]
        total = sum(net for net, _ in completed)
        best_net, best = max(completed, key=lambda x: x[0])
        result["books"][book] = {
            "closed": len(completed), "open": len(pending),
            "snapshot_equity_usd": float(equity),
            "start_usd": state["start_usd"],
            "snapshot_return_pct": float((equity / D(str(state["start_usd"])) - 1) * 100),
            "closed_net_from_prices_full_cycle_fees_funding": float(total),
            "by_side_net": {k: float(v) for k, v in by_side.items()},
            "by_module_net": {k: float(v) for k, v in by_module.items()},
            "best_trade": {"symbol": best["symbol"], "net": float(best_net), "time": best["time"]},
            "closed_net_without_best": float(total - best_net),
            "closed_mean_bars": statistics.mean(float(r["bars"]) for _, r in completed),
            "closed_median_bars": statistics.median(float(r["bars"]) for _, r in completed),
            "closed_max_bars": max(float(r["bars"]) for _, r in completed),
            "closed_fraction_at_most_24_bars": sum(float(r["bars"]) <= 24 for _, r in completed) / len(completed),
            "open_leverage_min": min(float(r["leverage"]) for r in opens),
            "open_leverage_max": max(float(r["leverage"]) for r in opens),
            "closed_entries_over_100_usd": sum(D(r["notional"]) > 100 for _, r in completed),
            "cash_reconciliation_rounding_residual_usd": float(residual),
        }
    return result


def inspect_rl(path):
    paths = ["results/holdout/holdout_best_model__2025/metrics.txt",
             "results/holdout/holdout_best_model__2026H1/metrics.txt"]
    result = {"commit": checkout(path, "rl"), "holdout": []}
    for p in paths:
        text = (path / p).read_text()
        balance = D(re.search(r"Final Balance:\s+\$([\d,.]+)", text)[1].replace(",", ""))
        trades = list(csv.DictReader((path / p).with_name("trades.csv").open()))
        count = int(re.search(r"Closed Trades:\s+(\d+)", text)[1])
        assert len(trades) == count
        result["holdout"].append({"path": p, "final_balance": float(balance),
                                   "metrics_sha256": sha(path / p),
                                   "trades_sha256": sha((path / p).with_name("trades.csv")),
                                   "closed_trades": count,
                                   "return_from_10000_pct": float((balance / D(10000) - 1) * 100)})
    balances = [D(str(x["final_balance"])) for x in result["holdout"]]
    result["conditional_compound_pct_from_two_reset_years"] = float((balances[0] / 10000 * balances[1] / 10000 - 1) * 100)
    result["compound_caveat"] = "Arithmetic illustration, not a continuous replay; both source accounts reset."
    return result


def inspect_sentinel(path):
    files = ["src/sentinel/execution/paper_broker.py", "src/sentinel/backtest.py",
             "scripts/carry_integration_bt.py", "README.md"]
    return {"commit": checkout(path, "sentinel"),
            "source_sha256": {f: sha(path / f) for f in files},
            "scope": "static review only; no backtest, API, broker or imported code executed"}


def run(args):
    return {"as_of_date_baku": "2026-11-11", "josiah": inspect_josiah(args.josiah),
            "rl": inspect_rl(args.rl), "sentinel": inspect_sentinel(args.sentinel),
            "input_sha256": {
                "josiah/" + f: sha(args.josiah / f) for f in
                ["state/trades.csv", "state/equity.csv", "state/live_state.json",
                 "bot/broker.py", "bot/engine.py", "bot/live.py", "bot/config.py"]}}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in PINS:
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    args.output.write_text(json.dumps(run(args), indent=2, sort_keys=True) + "\n")
