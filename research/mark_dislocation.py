"""Single offline experiment. See MARK_DISLOCATION_PROTOCOL.md."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import gzip
import json
import math
from pathlib import Path
from statistics import median

import numpy as np

from research import extreme_reversal as engine

SPLIT = engine.stamp("2026-01-01")
THRESHOLD = .004
HOLD = 15


def crossings(trade_close, mark_close, start, begin, end):
    if trade_close.shape != mark_close.shape:
        raise ValueError("unaligned minute arrays")
    valid = np.isfinite(trade_close) & np.isfinite(mark_close) & (mark_close > 0) & (trade_close > 0)
    gap = np.divide(trade_close - mark_close, mark_close, out=np.full(len(valid), np.nan), where=valid)
    previous, current = gap[:-1], gap[1:]
    observed = valid[:-1] & valid[1:]
    times = start + np.arange(2, len(gap) + 1) * engine.M
    in_period = (times >= begin) & (times < end)
    crossed = ((current <= -THRESHOLD) & (previous > -THRESHOLD)
               | (current >= THRESHOLD) & (previous < THRESHOLD))
    signals = []
    for j in np.flatnonzero(in_period & observed & crossed):
        signals.append(dict(signal_ms=int(times[j]), direction="LONG" if current[j] < 0 else "SHORT",
                            gap_bps=float(current[j] * 1e4)))
    return signals, int((in_period & observed).sum()), int((in_period & ~observed).sum())


def simulate(signals, trade, mark, funding, symbol, stress, *, hold_minutes=HOLD):
    delay = (6 if stress else 1) * engine.M
    busy_through = -1
    halted = False
    account = dict(cash=1000., peak=1000., max_dd=0.)
    records = []
    for signal in signals:
        t = signal["signal_ms"]
        record = dict(signal)
        if halted:
            record["rejection"] = "prior_position_unresolved"
        elif t <= busy_through:
            record["ignored"] = "position_or_pending"
        elif t + delay + hold_minutes * engine.M >= engine.END:
            record["rejection"] = "incomplete_final_horizon"
        else:
            i = engine.ix(t + delay)
            ref, mp = trade[i, 0], mark[i, 0]
            if "gap_bps" in signal and np.isfinite(ref) and np.isfinite(mp) and mp > 0:
                side = 1 if signal["direction"] == "LONG" else -1
                record["remaining_gap_bps"] = float(side * (mp - ref) / mp * 1e4)
            record.update(engine.execute(signal, trade, mark, funding, account, symbol, stress,
                                         hold_minutes=hold_minutes))
            busy_through = record.get("exit_ms", t + delay)
            halted = "unresolved" in record
        records.append(record)
    return records, account


def summarize(rows):
    trades = [r for r in rows if "net" in r]
    result = engine.summarize(rows, dict(cash=None, max_dd=None))
    result.pop("ignored_open")
    result.pop("ending_cash")
    result.pop("max_drawdown")
    daily = defaultdict(float)
    for r in trades:
        daily[datetime.fromtimestamp(r["signal_ms"] / 1000, timezone.utc).date().isoformat()] += r["net"]
    result.update(signals=len(rows), ignored=sum("ignored" in r for r in rows),
                  long=sum(r["direction"] == "LONG" for r in trades),
                  short=sum(r["direction"] == "SHORT" for r in trades), days=len(daily),
                  net_without_best_day=result["net"] - max(daily.values(), default=0),
                  daily_net=daily)
    if trades:
        result["signal_gap_bps_median_closed"] = median(abs(r["gap_bps"]) for r in trades)
        result["arrival_gap_bps_median_closed"] = median(r["remaining_gap_bps"] for r in trades)
    return result


def run(root, out):
    out.mkdir(parents=True, exist_ok=True)
    pinned = json.loads(Path(__file__).with_name("btc_eth_delay_results").joinpath("inputs.json").read_text())
    reports = {}
    for symbol in ("BTCUSDT", "ETHUSDT"):
        trade, mark, funding, files, overlap = engine.load_data(root, symbol)
        if files != pinned[symbol]["files"] or overlap != pinned[symbol]["archive_rest_differences"]:
            raise ValueError("data differ from frozen inputs")
        signals, valid, missing = crossings(trade[:, 3], mark[:, 3], engine.START, engine.BEGIN, engine.END)
        by_minute = defaultdict(list)
        for t, rate in funding:
            by_minute[engine.ix(t - t % engine.M)].append((t, rate))
        for scenario in ("base", "stress"):
            stress = scenario == "stress"
            rows, account = simulate(signals, trade, mark, by_minute, symbol, stress)
            summary = summarize(rows)
            periods = {label: summarize([r for r in rows if begin <= r["signal_ms"] < end])
                       for label, begin, end in (("2025", engine.BEGIN, SPLIT),
                                                 ("2026", SPLIT, engine.END))}
            checks = dict(drawdown=account["max_dd"] <= .02, resolved=not summary["unresolved"])
            for label, group in periods.items():
                checks[label + "_count"] = group["closed"] >= 30
                checks[label + "_sides"] = min(group["long"], group["short"]) >= 10
                checks[label + "_net"] = group["net"] > 0
                if stress:
                    checks[label + "_ex_best_day"] = group["net_without_best_day"] > 0
            if checks["resolved"] and not math.isclose(1000 + summary["net"], account["cash"], abs_tol=1e-8):
                raise ValueError("cash reconciliation")
            name = f"{symbol}-{scenario}"
            reports[name] = dict(complete_minutes=valid, invalid_minutes=missing, account=account,
                                 all=summary, periods=periods, checks=checks)
            (out / f"{name}.json.gz").write_bytes(gzip.compress(json.dumps(rows, allow_nan=False).encode(), mtime=0))
    (out / "summary.json").write_text(json.dumps(reports, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.archive, args.output)
