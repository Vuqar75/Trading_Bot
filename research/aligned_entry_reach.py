"""Check strict trade-through feasibility of the aligned passive candidates."""
import argparse
from collections import Counter
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research import trend_alignment_coverage as coverage

core = coverage.core
STEP = Decimal(".001")


def archive_window(trades, arm, deadline):
    times = trades.transact_time.to_numpy()
    i = times.searchsorted(arm, side="right")
    j = times.searchsorted(deadline, side="left")
    if i == 0 or j >= len(trades):
        return None
    ids = trades.agg_trade_id.iloc[i-1:j+1].to_numpy()
    if not np.all(np.diff(ids) == 1):
        return None
    return trades.iloc[i:j]


def inspect_candidate(candidate, arm_quote, trades):
    if arm_quote is None:
        return dict(status="unconfirmed_arm_quote")
    entry = Decimal(candidate["entry"])
    long = candidate["direction"] == "LONG"
    if (long and entry >= Decimal(arm_quote["ask"])) or (
        not long and entry <= Decimal(arm_quote["bid"])
    ):
        return dict(status="limit_would_take")
    if trades is None:
        return dict(status="unconfirmed_trade_window")
    qty = Decimal(candidate["quantity_cap"])
    filled = Decimal(0)
    threshold = float(entry)
    prices = trades.price
    candidates = trades.loc[(prices.lt(threshold) if long else prices.gt(threshold))
                           & trades.is_buyer_maker.eq(long)]
    first_id = None
    full_id = None
    for row in candidates.itertuples():
        amount = (Decimal(str(row.quantity))/STEP).to_integral_value(rounding=ROUND_DOWN)*STEP
        if amount <= 0:
            continue
        if first_id is None:
            first_id = int(row.agg_trade_id)
        filled += min(amount, qty-filled)
        if filled == qty:
            full_id = int(row.agg_trade_id)
            break
    result = dict(
        status="full_trade_through" if full_id else "partial_trade_through" if first_id else "no_trade_through",
        touched=bool(prices.le(threshold).any() if long else prices.ge(threshold).any()),
        price_extreme=(float(prices.min() if long else prices.max()) if len(prices) else None),
        possible_qty=str(filled), first_trade_id=first_id, full_trade_id=full_id
    )
    return result


def run(price, quotes, trades, coverage_results, out):
    trade, mark, _, sources, overlaps = core.load_data(price, "BTCUSDT")
    previous = json.loads((coverage_results/"samples.json").read_text())
    original = {r["time_ms"]:r for r in previous if any(r.get("after", {}).values())}
    quotes_manifest = {r["day"]:r for r in json.loads((quotes/"manifest.json").read_text())}
    trades_manifest = json.loads((trades/"manifest.json").read_text())
    matches = {}
    for item in trades_manifest:
        day = item["day"]
        quote_source = quotes_manifest[day]
        qp, tp = quotes/quote_source["file"], trades/item["file"]
        for path, receipt in ((qp, quote_source), (tp, item)):
            if hashlib.sha256(path.read_bytes()).hexdigest() != receipt["sha256"]:
                raise ValueError("Input checksum mismatch")
        ticks = pd.read_csv(tp, usecols=["agg_trade_id", "transact_time", "price", "quantity", "is_buyer_maker"])
        if not ticks.transact_time.is_monotonic_increasing or not pd.api.types.is_bool_dtype(ticks.is_buyer_maker):
            raise ValueError("Unusable trade tape")
        for sample in coverage.sample(qp, day, trade, mark, include_plans=True):
            time = sample["time_ms"]
            if time not in original:
                continue
            if any(sample[key] != original[time][key] for key in ("before","after","alignment")):
                raise ValueError("Candidate reproduction disagreement")
            arm = time+60_000
            deadline = (time//coverage.plan.BAR+1)*coverage.plan.BAR
            window = archive_window(ticks, arm, deadline)
            candidates = [dict(plan, **inspect_candidate(plan, sample.get("arm_quote"), window))
                          for plan in sample["candidates"]]
            matches[time] = dict(arm_ms=arm, deadline_ms=deadline, n_public_trades=len(window) if window is not None else None,
                                 arm_quote=sample.get("arm_quote"), candidates=candidates)
    if set(matches) != set(original):
        raise ValueError("Not all planned snapshots were replayed")
    all_plans = [r for snap in matches.values() for r in snap["candidates"]]
    result = dict(
        n_snapshots=len(matches), n_plans=len(all_plans),
        snapshots_with_full_trade_through=sum(
            any(r["status"]=="full_trade_through" for r in snap["candidates"]) for snap in matches.values()
        ),
        status=Counter(r["status"] for r in all_plans),
        sides={side:Counter(r["status"] for r in all_plans if r["direction"]==side)
               for side in ("LONG","SHORT")},
        quote_sources=[quotes_manifest[r["day"]] for r in trades_manifest], trades_sources=trades_manifest,
        price_sources=sources, overlaps=overlaps,
    )
    out.mkdir(parents=True, exist_ok=True)
    (out/"summary.json").write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")
    (out/"snapshots.json").write_text(json.dumps(matches,indent=2,allow_nan=False)+"\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("price", "quotes", "trades", "coverage", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    run(args.price, args.quotes, args.trades, args.coverage, args.output)
