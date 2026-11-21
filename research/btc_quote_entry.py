"""Compare fixed historical entry prices with archived best bid and ask."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.btc_quote_archive import SECOND, distribution

ORIGIN_MS = pd.Timestamp("2024-12-01", tz="UTC").value // 1_000_000
IMPACT = {0: .0005, 1: .001}


def reprice(trades, quotes):
    event = quotes.timestamp.to_numpy()
    received = quotes.local_timestamp.to_numpy()
    if np.any(np.diff(received) < 0):
        raise ValueError("Quotes are out of receive order")
    if not quotes.exchange.eq("binance-futures").all() or not quotes.symbol.eq("BTCUSDT").all():
        raise ValueError("Unexpected instrument")
    results = []
    for trade in trades.itertuples():
        side = int(trade.side)
        time = int(trade.entry_ms) * 1000
        row = dict(time_us=time, signal_ms=int(trade.ts), side=side, scenario=int(trade.scenario),
                   quantity=float(trade.q), modeled_entry=float(trade.entry),
                   reference=float(trade.entry / (1 + side * IMPACT[trade.scenario])))
        idx = int(np.searchsorted(received, time, side="right")) - 1
        if idx < 0:
            results.append(dict(row, reject="quote_missing"))
            continue
        if event[idx] > received[idx] or time - event[idx] > 5 * SECOND:
            results.append(dict(row, reject="quote_time"))
            continue
        q = quotes.iloc[idx]
        if not np.isfinite([q.bid_price, q.ask_price, q.bid_amount, q.ask_amount]).all() or not (
            0 < q.bid_price <= q.ask_price and min(q.bid_amount, q.ask_amount) > 0
        ):
            results.append(dict(row, reject="quote_invalid"))
            continue
        price, size = (q.ask_price, q.ask_amount) if side == 1 else (q.bid_price, q.bid_amount)
        if size < trade.q or not 50 <= price * trade.q <= 100:
            results.append(dict(row, reject="quantity_or_notional"))
            continue
        mid = (q.ask_price + q.bid_price) / 2
        row.update(
            received_us=int(q.local_timestamp), exchange_us=int(q.timestamp),
            bid=float(q.bid_price), ask=float(q.ask_price), available_at_side=float(size),
            quote_entry=float(price), basis_vs_reference_bp=float(side*(price/row["reference"]-1)*10_000),
            mid_vs_reference_bp=float(side*(mid/row["reference"]-1)*10_000),
            quote_vs_modeled_bp=float(side*(price/trade.entry-1)*10_000),
            entry_savings_usdt=float(side*(trade.entry-price)*trade.q),
        )
        results.append(row)
    return results


def report(rows):
    matched = [r for r in rows if "quote_entry" in r]
    return dict(
        requested=len(rows), matched=len(matched), rejects=Counter(r["reject"] for r in rows if "reject" in r),
        basis_vs_reference_bp=distribution([r["basis_vs_reference_bp"] for r in matched]),
        mid_vs_reference_bp=distribution([r["mid_vs_reference_bp"] for r in matched]),
        quote_vs_modeled_bp=distribution([r["quote_vs_modeled_bp"] for r in matched]),
        savings_usdt=sum(r["entry_savings_usdt"] for r in matched),
        quote_worse_than_modeled=sum(r["entry_savings_usdt"] < 0 for r in matched),
    )


def run(fill_file, root, output):
    output.mkdir(parents=True, exist_ok=True)
    entries = pd.read_csv(fill_file)
    entries = entries.loc[entries.variant.eq("baseline")].copy()
    entries["entry_ms"] = ORIGIN_MS + entries.i * 60000
    expected = entries.ts + entries.scenario.map({0: 60000, 1: 360000})
    if not entries.entry_ms.eq(expected).all() or not entries.side.isin((-1, 1)).all():
        raise ValueError("Archived entry minute or side mismatch")
    entries["day"] = pd.to_datetime(entries.entry_ms, unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    days = {}
    all_rows = []
    manifest = json.loads((root / "manifest.json").read_text())
    if [r["day"] for r in manifest] != [f"2026-{m:02d}-01" for m in range(1, 11)]:
        raise ValueError("Unexpected source sample dates")
    for source in manifest:
        day = source["day"]
        selected = entries.loc[entries.day.eq(day)]
        if "error" in source:
            rows = [dict(time_us=int(t.entry_ms)*1000, scenario=int(t.scenario), reject="archive_missing")
                    for t in selected.itertuples()]
        else:
            path = root / source["file"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
                raise ValueError("Input hash mismatch")
            rows = reprice(selected, pd.read_csv(path))
        all_rows.extend(rows)
        days[day] = dict(source=source, **report(rows))
    result = dict(baseline_entries=len(entries), source_sha256=hashlib.sha256(fill_file.read_bytes()).hexdigest(),
                  days=days, totals={n: report([r for r in all_rows if r["scenario"] == code])
                                    for code, n in ((0, "base"), (1, "stress"))})
    (output / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    (output / "entries.json").write_text(json.dumps(all_rows, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("fills", "archive", "output"):
        parser.add_argument(key, type=Path)
    args = parser.parse_args()
    run(args.fills, args.archive, args.output)
