"""Offline BTC dated-futures basis feasibility on checksum-verified archives."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np

from research import btc_spot_perp_carry as carry
from research import extreme_reversal as core

EXPIRIES = ("250328", "250627", "250926", "251226", "260327", "260626", "260925")
SCENARIOS = {"base": (1, .0005), "stress": (6, .001)}
SPOT_FEE = carry.SPOT_FEE
FUTURE_FEE = carry.PERP_FEE


def contract_prices(root, files, start, end):
    arrays = {kind: np.full(((end - start) // core.M + 1, 4), np.nan)
              for kind in ("klines", "markPriceKlines")}
    for item in files:
        path = root / item["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"Input checksum mismatch: {path.name}")
        result = arrays[item["kind"]]
        for row in core.zipped(path):
            if not row[0].isdigit():
                continue
            time = int(row[0])
            if not start <= time <= end:
                continue
            i = (time - start) // core.M
            if time % core.M or int(row[6]) != time + core.M - 1 or np.isfinite(result[i, 0]):
                raise ValueError("Invalid contract timestamp")
            o, h, l, c = map(float, row[1:5])
            if not 0 < l <= min(o, c) <= max(o, c) <= h < float("inf"):
                raise ValueError("Invalid contract price")
            result[i] = o, h, l, c
    return arrays["klines"], arrays["markPriceKlines"]


def economics(s0, f0, s1, f1, impact):
    entry_spot, exit_spot = s0 * (1 + impact), s1 * (1 - impact)
    entry_future, exit_future = f0 * (1 - impact), f1 * (1 + impact)
    entry_basis, exit_basis = f0 - s0, f1 - s1
    fees = (entry_spot + exit_spot) * SPOT_FEE + (entry_future + exit_future) * FUTURE_FEE
    spread = impact * (s0 + s1 + f0 + f1)
    gross = entry_basis - exit_basis
    return dict(entry_spot=entry_spot, exit_spot=exit_spot, entry_future=entry_future,
                exit_future=exit_future, entry_basis=entry_basis, exit_basis=exit_basis,
                gross=gross, fees=fees, impact=spread, net=gross-fees-spread,
                net_bp=(gross-fees-spread) / s0 * 10_000)


def size(s, f, mark, balance):
    step = Decimal(".001")
    qty = float((Decimal(100) / Decimal(str(max(s, f, mark))) / step)
                .to_integral_value(rounding=ROUND_DOWN) * step)
    if min(f, mark)*qty < 50 or s*qty < 5:
        return 0, "minimum_or_cap"
    if qty * (s * (1 + SPOT_FEE) + f * (1 + FUTURE_FEE)) > balance:
        return 0, "capital"
    return qty, None


def equity_bounds(s, f, spot, future, mark, i, qty, account, row, impact):
    initial = account["balance"]
    if np.any(2 * f - mark[i:-1, 1] * 1.005 - f * FUTURE_FEE <= 0):
        return False
    carry_value = f * (1-FUTURE_FEE) - s * (1+SPOT_FEE)
    lower = initial + qty * (carry_value + spot[i:-1, 2]*(1-impact)*(1-SPOT_FEE)
                             - future[i:-1, 1]*(1+impact)*(1+FUTURE_FEE))
    upper = initial + qty * (carry_value + spot[i:-1, 1]*(1-impact)*(1-SPOT_FEE)
                             - future[i:-1, 2]*(1+impact)*(1+FUTURE_FEE))
    peaks = np.maximum.accumulate(np.maximum(upper, account["peak"]))
    balance = initial + qty * row["net"]
    peak = max(float(peaks[-1]), balance)
    account.update(balance=balance, peak=peak, drawdown=max(
        account["drawdown"], float(np.max(1-lower/peaks)), 1-balance/peak
    ))
    return True


def evaluate(expiry, start, end, spot, future, mark, impact, delay, account):
    journal = []
    traded = False
    for days in range(28, 6, -1):
        time = int((expiry - timedelta(days=days, minutes=-15)).timestamp() * 1000)
        signal_at = (time - start) // core.M - 1
        i = signal_at + 1 + delay
        anchor = [spot[signal_at, 3], future[signal_at, 3]]
        row = dict(days=days, signal_ms=time, entry_ms=time + delay*core.M, exit_ms=end)
        if not np.isfinite(anchor).all():
            journal.append(dict(row, availability="signal_missing"))
            continue
        s0, f0, m0 = spot[i, 0], future[i, 0], mark[i, 0]
        row["signal_basis_bp"] = (anchor[1]/anchor[0]-1)*10_000
        row["signal"] = bool(row["signal_basis_bp"] >= 80 - 1e-10)
        if not np.isfinite([s0, f0, m0]).all():
            journal.append(dict(row, availability="entry_missing"))
            continue
        qty, rejected = size(s0*(1+impact), f0*(1-impact), m0, account["balance"])
        row["eligible_quantity"] = qty
        complete = all(np.isfinite(a[i:]).all() for a in (spot, future, mark))
        if complete:
            row.update(economics(s0, f0, spot[-1, 0], future[-1, 0], impact))
        else:
            row["availability"] = "incomplete_path"
        if row["signal"] and not traded and not account["unresolved"]:
            if rejected:
                row["rejection"] = rejected
            elif not complete:
                account["unresolved"] = "incomplete_path"
                row["unresolved"] = account["unresolved"]
            elif equity_bounds(row["entry_spot"], row["entry_future"], spot, future, mark,
                               i, qty, account, row, impact):
                traded = True
                row.update(qty=qty, balance=account["balance"])
            else:
                account["unresolved"] = "possible_liquidation"
                row["unresolved"] = account["unresolved"]
        journal.append(row)
    return journal


def summarize(contracts, account):
    rows = [row for journal in contracts.values() for row in journal]
    complete = [row for row in rows if "net" in row]
    trades = [row for row in rows if "qty" in row]
    pnl = [row["qty"] * row["net"] for row in trades]
    total = sum(pnl)
    if abs(account["balance"] - 1000 - total) > 1e-8:
        raise ValueError("Unreconciled account")
    return dict(windows=len(rows), complete=len(complete), signals=sum(row.get("signal", False) for row in rows),
                eligible_signals=sum(row.get("signal", False) and row.get("eligible_quantity", 0) > 0 for row in rows),
                profitable_windows=sum(1 for row in complete if row["net"] > 0),
                best_bp=max((row["net_bp"] for row in complete), default=None),
                best_signal_basis_bp=max((row["signal_basis_bp"] for row in rows if "signal" in row), default=None),
                d7_profitable=sum(1 for row in complete if row["net"] > 0 and row["days"] == 7),
                n=len(trades), net=total, without_best=total-max(pnl, default=0),
                drawdown=account["drawdown"], unresolved=account["unresolved"],
                rejections=Counter(row["rejection"] for row in rows if "rejection" in row))


def run(spot_root, contracts_root, out):
    spot, spot_sources = carry.load_spot(spot_root)
    manifest = json.loads((contracts_root / "manifest.json").read_text())
    if any("error" in r for r in manifest):
        raise ValueError("Contract archive incomplete")
    out.mkdir(parents=True, exist_ok=True)
    accounts = {name: dict(balance=1000., peak=1000., drawdown=0., unresolved=None)
                for name in SCENARIOS}
    journals = {name: {} for name in SCENARIOS}
    for code in EXPIRIES:
        expiry = datetime.strptime(code, "%y%m%d").replace(tzinfo=timezone.utc)
        start = int((expiry-timedelta(days=28)).timestamp()*1000)
        end = int((expiry+timedelta(hours=7, minutes=45)).timestamp()*1000)
        symbol = "BTCUSDT_" + code
        sources = [row for row in manifest if row["symbol"] == symbol]
        future, mark = contract_prices(contracts_root, sources, start, end)
        sample_spot = spot[core.ix(start):core.ix(end)+1]
        for name, (delay, impact) in SCENARIOS.items():
            journals[name][symbol] = evaluate(expiry, start, end, sample_spot, future, mark,
                                              impact, delay, accounts[name])
    summaries = {name: summarize(journal, accounts[name]) for name, journal in journals.items()}
    for name, journal in journals.items():
        raw = json.dumps(journal, allow_nan=False).encode()
        (out / f"{name}.json.gz").write_bytes(gzip.compress(raw, mtime=0))
    payload = dict(cases=summaries, spot_sources=spot_sources, contract_sources=manifest)
    (out / "summary.json").write_text(json.dumps(payload, indent=2, allow_nan=False)+"\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("spot", "contracts", "output"):
        parser.add_argument(key, type=Path)
    args = parser.parse_args()
    run(args.spot, args.contracts, args.output)
