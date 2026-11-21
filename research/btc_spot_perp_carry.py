"""Offline synchronized BTC spot/perpetual carry calculation."""
import argparse
from bisect import bisect_left, bisect_right
from collections import Counter
from decimal import Decimal, ROUND_DOWN
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np

from research import extreme_reversal as core
from research.funding_income import SLOT
from research.mark_dislocation import SPLIT

HOLD = 7 * 24 * 60
PAYMENTS = 21
SPOT_FEE = .001
PERP_FEE = .0005


def load_spot(root):
    files = json.loads((root / "manifest.json").read_text())
    bars = np.full((core.N, 4), np.nan)
    for item in files:
        path = root / item["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"Spot checksum mismatch: {path.name}")
        for row in core.zipped(path):
            opened, closed = int(row[0]), int(row[6])
            if opened >= 10**15:
                if closed != opened + 59_999_999:
                    raise ValueError("Invalid spot microsecond interval")
                opened //= 1000
                closed //= 1000
            if opened % core.M or closed != opened + core.M - 1:
                raise ValueError("Invalid spot minute interval")
            i = core.ix(opened)
            if not 0 <= i < len(bars) or np.isfinite(bars[i, 0]):
                raise ValueError("Unexpected or duplicate spot timestamp")
            o, h, l, c = map(float, row[1:5])
            if not 0 < l <= min(o, c) <= max(o, c) <= h < float("inf"):
                raise ValueError("Invalid spot OHLC")
            bars[i] = o, h, l, c
    return bars, files


def signal_at(slot, spot, perp, funding, times):
    time = slot * SLOT + 15 * core.M
    i = core.ix(time) - 1
    s, p = spot[i, 3], perp[i, 3]
    k = bisect_right(times, time)
    prior = funding[max(0, k - PAYMENTS):k]
    if (not np.isfinite([s, p]).all() or
        [t // SLOT for t, _ in prior] != list(range(slot - PAYMENTS + 1, slot + 1))):
        return None
    past = sum(Decimal(str(rate)) for _, rate in prior)
    basis = Decimal(str(p)) / Decimal(str(s)) - 1
    score = basis + past
    return dict(signal_ms=time, basis_bp=float(basis * 10_000),
                past_funding_bp=float(past * 10_000), score_bp=float(score * 10_000),
                entry_signal=bool(past > 0 and score >= Decimal(".007")))


def window(signal, spot, perp, mark, funding, times, delay, impact):
    entry = signal["signal_ms"] + delay * core.M
    exit_time = entry + HOLD * core.M
    i, j = core.ix(entry), core.ix(exit_time)
    if not (np.isfinite(spot[i:j + 1]).all() and np.isfinite(perp[i:j + 1]).all()
            and np.isfinite(mark[i:j + 1]).all()):
        return None
    events = funding[bisect_right(times, entry):bisect_left(times, exit_time)]
    if len(events) != PAYMENTS:
        return None
    sf, sx = spot[i, 0], spot[j, 0]
    pf, px = perp[i, 0], perp[j, 0]
    s_in, s_out = sf * (1 + impact), sx * (1 - impact)
    p_in, p_out = pf * (1 - impact), px * (1 + impact)
    applied = [(t, float(mark[core.ix(t - t % core.M), 0] * rate)) for t, rate in events]
    funding_amount = sum(value for _, value in applied)
    gross = sx - sf + pf - px
    fees = (s_in + s_out) * SPOT_FEE + (p_in + p_out) * PERP_FEE
    impact_amount = abs(s_in - sf) + abs(s_out - sx) + abs(p_in - pf) + abs(p_out - px)
    net = gross + funding_amount - fees - impact_amount
    return dict(signal, entry_ms=entry, exit_ms=exit_time, spot_reference=float(sf),
                spot_entry=float(s_in), perp_entry=float(p_in), mark_entry=float(mark[i, 0]),
                spot_exit=float(s_out), perp_exit=float(p_out), gross=float(gross),
                fees=float(fees), impact=float(impact_amount), funding=float(funding_amount),
                net=float(net), net_bp=float(net / sf * 10_000), events=applied)


def replay(windows, spot, perp, mark, impact):
    balance = peak = 1000.
    drawdown = 0.
    busy_until = -1
    trades = []
    rejected = Counter()
    for row in windows:
        if not row["entry_signal"]:
            continue
        if row["signal_ms"] <= busy_until:
            rejected["busy"] += 1
            continue
        prices = [row[key] for key in ("spot_entry", "perp_entry", "mark_entry")]
        step = Decimal(".001")
        qty = float((Decimal("100") / Decimal(str(max(prices))) / step)
                    .to_integral_value(rounding=ROUND_DOWN) * step)
        s_in, p_in, mark_in = prices
        if qty * min(p_in, mark_in) < 50 or qty * s_in < 5:
            rejected["minimum_or_cap"] += 1
            continue
        paid = s_in * SPOT_FEE + p_in * PERP_FEE
        if qty * (s_in + p_in + paid) > balance:
            rejected["capital"] += 1
            continue
        i, j = (core.ix(row[key]) for key in ("entry_ms", "exit_ms"))
        increments = np.zeros(j - i)
        for time, value in row["events"]:
            # Delay credits until after the funding minute, book debits before it.
            k = core.ix(time - time % core.M) - i + (value > 0)
            if k < len(increments):
                increments[k] += value
        accrued = np.cumsum(increments)
        mark_high = mark[i:j, 1]
        if np.any(2 * p_in - mark_high * 1.005 - p_in * PERP_FEE + accrued <= 0):
            rejected["possible_perp_liquidation"] += 1
            return trades, balance, drawdown, rejected
        spot_low, spot_high = spot[i:j, 2] * (1 - impact), spot[i:j, 1] * (1 - impact)
        perp_low, perp_high = perp[i:j, 2] * (1 + impact), perp[i:j, 1] * (1 + impact)
        carry = p_in - s_in - paid + accrued
        lower = balance + qty * (carry + spot_low * (1 - SPOT_FEE) - perp_high * (1 + PERP_FEE))
        upper = balance + qty * (carry + spot_high * (1 - SPOT_FEE) - perp_low * (1 + PERP_FEE))
        peaks = np.maximum.accumulate(np.maximum(upper, peak))
        drawdown = max(drawdown, float(np.max(1 - lower / peaks)))
        balance += qty * row["net"]
        peak = max(float(peaks[-1]), balance)
        drawdown = max(drawdown, 1 - balance / peak)
        busy_until = row["exit_ms"]
        trades.append(dict(row, qty=qty, balance=balance))
    return trades, balance, drawdown, rejected


def totals(trades):
    amounts = {key: sum(r["qty"] * r[key] for r in trades)
               for key in ("gross", "fees", "impact", "funding", "net")}
    without_best = amounts["net"] - max((r["qty"] * r["net"] for r in trades), default=0)
    return dict(amounts, n=len(trades), without_best=without_best)


def diagnostics(rows):
    matched = [row for row in rows if row["entry_signal"]]
    return dict(complete=len(rows), profitable=sum(row["net"] > 0 for row in rows),
                best_bp=max((row["net_bp"] for row in rows), default=None),
                causal_signals=len(matched), causal_profitable=sum(row["net"] > 0 for row in matched),
                best_score_bp=max((row["score_bp"] for row in rows), default=None))


def run(spot_root, perp_root, output):
    spot, spot_sources = load_spot(spot_root)
    perp, mark, funding, perp_sources, conflicts = core.load_data(perp_root, "BTCUSDT")
    times = [t for t, _ in funding]
    output.mkdir(parents=True, exist_ok=True)
    result = dict(spot_sources=spot_sources, perp_sources=perp_sources, conflicts=conflicts, cases={})
    for name, delay, impact in (("base", 1, .0005), ("stress", 6, .001)):
        rows, missing = [], []
        for slot in range(core.BEGIN // SLOT, core.END // SLOT):
            time = slot * SLOT + 15 * core.M
            if time + (delay + HOLD) * core.M >= core.END:
                break
            signal = signal_at(slot, spot, perp, funding, times)
            row = window(signal, spot, perp, mark, funding, times, delay, impact) if signal else None
            if row is None:
                missing.append(dict(signal_ms=time, causal_status=signal))
            else:
                rows.append(row)
        trades, balance, dd, rejected = replay(rows, spot, perp, mark, impact)
        total = totals(trades)
        if abs(balance - 1000 - total["net"]) > 1e-8:
            raise ValueError("Unreconciled account")
        result["cases"][name] = dict(total=total, balance=balance, drawdown=dd, rejected=rejected,
            availability_missing=len(missing), missing_causal=sum(
                r["causal_status"] is not None and r["causal_status"]["entry_signal"] for r in missing
            ), **diagnostics(rows), years={str(year): totals([
                r for r in trades if (r["signal_ms"] < SPLIT) == (year == 2025)
            ]) for year in (2025, 2026)})
        journal = dict(windows=rows, trades=trades, missing=missing)
        raw = json.dumps(journal, allow_nan=False).encode()
        (output / f"{name}.json.gz").write_bytes(gzip.compress(raw, mtime=0))
    (output / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("spot", "perp", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    run(args.spot, args.perp, args.output)
