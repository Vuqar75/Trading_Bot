"""Offline seven-day funding feasibility using pinned archive data."""
import argparse
from bisect import bisect_left, bisect_right
from collections import defaultdict
from decimal import Decimal
import gzip
import json
from pathlib import Path

import numpy as np

from research import extreme_reversal as core
from research.flow_weekend import metrics
from research.funding_income import SLOT
from research.mark_dislocation import SPLIT, simulate


PAYMENTS = 21
HOLD = 7 * 24 * 60
PERIODS = (("2025", core.BEGIN, SPLIT), ("2026", SPLIT, core.END))


def decisions(funding):
    times = [time for time, _ in funding]
    signals = []
    for slot in range(core.BEGIN // SLOT, core.END // SLOT):
        signal = slot * SLOT + 15 * core.M
        end = bisect_right(times, signal)
        past = funding[max(0, end - PAYMENTS):end]
        if [time // SLOT for time, _ in past] != list(range(slot - PAYMENTS + 1, slot + 1)):
            continue
        total = sum(Decimal(str(rate)) for _, rate in past)
        if abs(total) >= Decimal("0.003"):
            signals.append(dict(signal_ms=signal, direction="SHORT" if total > 0 else "LONG",
                                trailing_bps=float(total * 10_000), known_settlements=past))
    return signals


def windows(trade, mark, funding, signals, delay):
    times = [time for time, _ in funding]
    forecast = {row["signal_ms"]: row["direction"] for row in signals}
    for slot in range(core.BEGIN // SLOT, core.END // SLOT):
        signal = slot * SLOT + 15 * core.M
        entry = signal + delay * core.M
        exit_time = entry + HOLD * core.M
        if exit_time >= core.END:
            break
        row = {"signal_ms": signal}
        future = funding[bisect_left(times, entry):bisect_left(times, exit_time)]
        ref = trade[core.ix(entry), 0]
        amounts = [mark[core.ix(t - t % core.M), 0] * rate for t, rate in future]
        if len(amounts) != PAYMENTS or not ref > 0 or not np.isfinite(amounts).all():
            yield dict(row, incomplete=True)
            continue
        signed = float(sum(amounts) / ref * 10_000)
        side = forecast.get(signal)
        yield dict(row, best_fixed_side_bps=abs(signed),
                   switching_upper_bps=float(sum(map(abs, amounts)) / ref * 10_000),
                   forecast_side_bps=(signed if side == "SHORT" else -signed) if side else None)


def summarize_windows(rows):
    valid = [row for row in rows if "incomplete" not in row]
    result = {"complete": len(valid), "incomplete": len(rows) - len(valid)}
    for key in ("best_fixed_side_bps", "switching_upper_bps", "forecast_side_bps"):
        values = [row[key] for row in valid if row[key] is not None]
        result[key] = dict(count=len(values), maximum=max(values, default=None),
                           reaching={t: sum(value >= t for value in values) for t in (10, 20, 30)})
    return result


def run(root, output):
    output.mkdir(parents=True, exist_ok=True)
    results = {}
    for symbol in ("BTCUSDT", "ETHUSDT"):
        trade, mark, funding, files, overlaps = core.load_data(root, symbol)
        signals = decisions(funding)
        by_minute = defaultdict(list)
        for time, rate in funding:
            by_minute[core.ix(time - time % core.M)].append((time, rate))
        for scenario, delay in (("base", 1), ("stress", 6)):
            key = f"{symbol}-{scenario}"
            rows, account = simulate(
                signals, trade, mark, by_minute, symbol, scenario == "stress", hold_minutes=HOLD
            )
            window_rows = list(windows(trade, mark, funding, signals, delay))
            total = metrics(rows, account)
            if not total["unresolved"] and abs(account["cash"] - 1000 - total["net"]) > 1e-8:
                raise ValueError("ledger mismatch")
            periods = {}
            for name, start, end in PERIODS:
                segment = metrics([row for row in rows if start <= row["signal_ms"] < end])
                segment["windows"] = summarize_windows(
                    [row for row in window_rows if start <= row["signal_ms"] < end]
                )
                periods[name] = segment
            results[key] = dict(total=total, periods=periods)
            records = dict(records=rows, windows=window_rows, sources=files, overlaps=overlaps)
            (output / f"{key}.json.gz").write_bytes(
                gzip.compress(json.dumps(records, allow_nan=False).encode(), mtime=0)
            )
    (output / "summary.json").write_text(
        json.dumps(results, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.archive, args.output)
