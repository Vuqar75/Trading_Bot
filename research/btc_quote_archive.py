"""Inspect public BTCUSDT quote coverage and separate spread from time drift."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

SECOND = 1_000_000
MINUTE = 60 * SECOND
DAY = 1440 * MINUTE


def distribution(values):
    return dict(n=len(values), median=float(np.median(values)) if len(values) else None,
                p95=float(np.quantile(values, .95)) if len(values) else None)


def sample(path, day):
    frame = pd.read_csv(path)
    if not frame["exchange"].eq("binance-futures").all() or not frame["symbol"].eq("BTCUSDT").all():
        raise ValueError("Unexpected archive instrument")
    start = pd.Timestamp(day, tz="UTC").value // 1000
    event = frame["timestamp"].to_numpy()
    received = frame["local_timestamp"].to_numpy()
    bid = frame["bid_price"].to_numpy()
    ask = frame["ask_price"].to_numpy()
    sizes = frame[["bid_amount", "ask_amount"]].to_numpy()
    in_day = (event >= start) & (event < start + DAY)
    ordered = bool(np.all(np.diff(received) >= 0))
    if not ordered:
        raise ValueError("Quotes out of collector receive order")
    valid = (in_day & (event <= received) & np.isfinite(ask) & np.isfinite(bid)
             & (bid > 0) & (ask >= bid) & np.isfinite(sizes).all(axis=1) & (sizes > 0).all(axis=1))
    # Preserve rejected quotes in sequence so a bad latest update can't be ignored.
    rows = []
    targets = range(start, start + DAY, 15 * MINUTE)
    for t in targets:
        for lag in (0, 1, 6):
            target = t + lag * MINUTE
            i = int(np.searchsorted(received, target, side="right")) - 1
            row = dict(time_us=t, delay_min=lag, target_us=target)
            if i < 0 or not valid[i] or target - event[i] > 5 * SECOND:
                rows.append(dict(row, available=False))
                continue
            midpoint = float((ask[i] + bid[i]) / 2)
            row.update(available=True, exchange_us=int(event[i]), received_us=int(received[i]),
                       bid=float(bid[i]), ask=float(ask[i]), bid_qty=float(sizes[i, 0]),
                       ask_qty=float(sizes[i, 1]), mid=midpoint,
                       spread_bp=float((ask[i] - bid[i]) / midpoint * 10_000),
                       long_eligible=bool(sizes[i, 1] >= .001 and 50 <= ask[i] * .001 <= 100),
                       short_eligible=bool(sizes[i, 0] >= .001 and 50 <= bid[i] * .001 <= 100))
            rows.append(row)
    baseline = {row["time_us"]: row["mid"] for row in rows if row["available"] and row["delay_min"] == 0}
    delays = {}
    for lag in (0, 1, 6):
        selected = [r for r in rows if r["delay_min"] == lag and r["available"]]
        paired = [abs(r["mid"] / baseline[r["time_us"]] - 1) * 10_000
                  for r in selected if r["time_us"] in baseline]
        delays[lag] = dict(n=len(selected), spread_bp=distribution([r["spread_bp"] for r in selected]),
                           midpoint_drift_abs_bp=distribution(paired),
                           eligible_long=sum(r["long_eligible"] for r in selected),
                           eligible_short=sum(r["short_eligible"] for r in selected))
    summary = dict(rows=len(frame), valid_rows=int(valid.sum()), event_outside_utc_day=int((~in_day).sum()),
                   negative_receive_lag=int((event > received).sum()),
                   event_backward_steps=int((np.diff(event) < 0).sum()), receive_ordered=ordered,
                   first_event_us=int(event.min()), last_event_us=int(event.max()),
                   receive_lag_ms=distribution((received[valid] - event[valid]) / 1000),
                   delays=delays)
    return summary, rows


def run(root, out):
    out.mkdir(parents=True, exist_ok=True)
    summary = {}
    for row in json.loads((root / "manifest.json").read_text()):
        path = root / row["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Quote archive hash mismatch")
        stats, samples = sample(path, row["day"])
        summary[row["day"]] = dict(data=stats, source=row)
        (out / f'{row["day"]}-samples.json').write_text(json.dumps(samples, indent=2)+"\n")
    (out / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False)+"\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.archive, args.output)
