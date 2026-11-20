"""Frozen independent offline hypotheses W and F. No model fitting or network."""
import argparse
from collections import defaultdict
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np

from research import extreme_reversal as core
from research.mark_dislocation import simulate, SPLIT

DAY = 1440 * core.M


def read_volumes(root, proof):
    v = np.full((core.N, 2), np.nan)
    for item in proof:
        name = item["file"]
        if "/minute/klines/" not in "/" + name:
            continue
        path = root / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("volume source changed")
        for row in core.zipped(path):
            if not row[0].isdigit():
                if (row[0], row[5], row[9]) != ("open_time", "volume", "taker_buy_volume"):
                    raise ValueError("unexpected volume schema")
                continue
            i = core.ix(int(row[0]))
            total, bought = float(row[5]), float(row[9])
            if not 0 <= i < len(v) or np.isfinite(v[i, 0]) or not 0 <= bought <= total < np.inf:
                raise ValueError("invalid minute volume")
            v[i] = total, bought
    if not np.isfinite(v).all():
        raise ValueError("incomplete archived volume coverage")
    return v


def weekend_signals(closes, start, begin, end):
    signals = []
    # 1970-01-01 was a Thursday; UTC Monday has (days since epoch + 3) % 7 == 0.
    for i in range(2880, len(closes)):
        t = start + (i + 1) * core.M
        if t % DAY or not begin <= t < end or (t // DAY + 3) % 7:
            continue
        window = closes[i - 2880:i + 1]
        if not np.isfinite(window).all():
            continue
        change = float((window[-1] - window[0]) / window[0])
        if abs(change) >= .01:
            signals.append(dict(signal_ms=t, direction="SHORT" if change > 0 else "LONG",
                                weekend_return=change))
    return signals


def flow_signals(volumes, start, begin, end):
    if start % (15 * core.M) or len(volumes) % 15:
        raise ValueError("unaligned quarter-hour archive")
    blocks = volumes.reshape(-1, 3, 5, 2).sum(axis=2)
    total, bought = blocks[:, :, 0], blocks[:, :, 1]
    imbalance = np.divide(2 * bought - total, total, out=np.full_like(total, np.nan), where=total > 0)
    positive = (imbalance >= .2).all(axis=1)
    negative = (imbalance <= -.2).all(axis=1)
    signals = []
    for i in np.flatnonzero(positive | negative):
        t = int(start + (int(i) + 1) * 15 * core.M)
        if begin <= t < end:
            signals.append(dict(signal_ms=t, direction="LONG" if positive[i] else "SHORT",
                                imbalance=imbalance[i].tolist()))
    return signals


def metrics(rows, account=None):
    summary = core.summarize(rows, account or dict(max_dd=None, cash=None))
    days = defaultdict(float)
    closed = [r for r in rows if "net" in r]
    for r in closed:
        days[r["signal_ms"] // DAY] += r["net"]
    summary.pop("ignored_open")
    summary.update(signals=len(rows), busy=sum("ignored" in r for r in rows),
                   net_without_best_day=summary["net"] - max(days.values(), default=0),
                   sides={s: dict(count=sum(r["direction"] == s for r in closed),
                                  net=sum(r["net"] for r in closed if r["direction"] == s))
                          for s in ("LONG", "SHORT")})
    return summary


def run(root, out):
    out.mkdir(parents=True, exist_ok=True)
    inputs = json.loads(Path(__file__).with_name("btc_eth_delay_results").joinpath("inputs.json").read_text())
    results = {}
    for symbol, pinned in inputs.items():
        trade, mark, fund, files, overlaps = core.load_data(root, symbol)
        if files != pinned["files"] or overlaps != pinned["archive_rest_differences"]:
            raise ValueError("input revision")
        funding = defaultdict(list)
        for t, rate in fund:
            funding[core.ix(t - t % core.M)].append((t, rate))
        signals_w = weekend_signals(trade[:, 3], core.START, core.BEGIN, core.END)
        signals_f = flow_signals(read_volumes(root, files), core.START, core.BEGIN, core.END)
        for family, signals, holding in (("W", signals_w, 240), ("F", signals_f, 30)):
            for stress in (False, True):
                key = f"{family}-{symbol}-" + ("stress" if stress else "base")
                rows, account = simulate(signals, trade, mark, funding, symbol, stress, hold_minutes=holding)
                all_rows = metrics(rows, account)
                gates = dict(resolved=not all_rows["unresolved"], drawdown=account["max_dd"] <= .02)
                if gates["resolved"] and abs(1000 + all_rows["net"] - account["cash"]) > 1e-7:
                    raise ValueError("unreconciled ending cash")
                periods = {}
                for label, start, end in (("2025", core.BEGIN, SPLIT), ("2026", SPLIT, core.END)):
                    group = metrics([r for r in rows if start <= r["signal_ms"] < end])
                    periods[label] = group
                    gates[label + "_count"] = group["closed"] >= 30
                    gates[label + "_sides"] = min(x["count"] for x in group["sides"].values()) >= 10
                    gates[label + "_net"] = group["net"] > 0
                    if stress:
                        gates[label + "_without_best_day"] = group["net_without_best_day"] > 0
                results[key] = dict(all=all_rows, periods=periods, gates=gates)
                payload = json.dumps(rows, separators=(",", ":"), allow_nan=False).encode()
                (out / f"{key}.json.gz").write_bytes(gzip.compress(payload, mtime=0))
    (out / "summary.json").write_text(json.dumps(results, indent=2, allow_nan=False) + "\n", encoding="utf8")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("archive", type=Path)
    p.add_argument("output", type=Path)
    args = p.parse_args()
    run(args.archive, args.output)
