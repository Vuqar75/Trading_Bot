"""Measure coverage of the installed trend gate on closed historical candles."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research import ai_limit_plan as plan, extreme_reversal as core

HOUR = 60 * core.M


def trend_frames(trade, duration):
    minutes = duration // core.M
    bars = trade.reshape(-1, minutes, 4)
    valid = np.isfinite(bars).all(axis=(1, 2))
    closes = bars[:, -1, 3]
    frames = {}
    for i in range(39, len(closes)):
        if not valid[i - 39:i + 1].all():
            continue
        window = [plan.dec(str(v)) for v in closes[i - 39:i + 1]]
        time = core.START + (i + 1) * duration
        frames[time] = dict(closed_through_ms=time, close=plan.text(window[-1]),
                            sma20=plan.text(sum(window[-20:])/20),
                            sma40=plan.text(sum(window)/40))
    return frames


def history(trade):
    fast = trend_frames(trade, HOUR)
    slow = trend_frames(trade, 4 * HOUR)
    hourly, scheduled = [], []
    for time in range(core.BEGIN, core.END, HOUR):
        slow_time = time // (4 * HOUR) * (4 * HOUR)
        alignment = None
        if time in fast and slow_time in slow:
            alignment = plan.trend_alignment({"1h": fast[time], "4h": slow[slow_time]}, time+120000)
        row = dict(time_ms=time+120000, alignment=alignment)
        hourly.append(row)
        if time == slow_time:
            scheduled.append(row)
    return scheduled, hourly


def counts(rows):
    values = Counter(
        (row["alignment"]["allowed"] or "WAIT") if row["alignment"] else "UNAVAILABLE"
        for row in rows
    )
    return dict(n=len(rows), **{label: values[label] for label in ("LONG", "SHORT", "WAIT", "UNAVAILABLE")})


def captures(trade, close_time):
    i = core.ix(close_time)
    bars = trade[i - 960*15:i].reshape(-1, 15, 4)
    if not np.isfinite(bars).all():
        return None
    rows = [[close_time-(960-k)*plan.BAR, plan.text(plan.dec(str(bar[0, 0]))),
             plan.text(plan.dec(str(bar[:, 1].max()))), plan.text(plan.dec(str(bar[:, 2].min()))),
             plan.text(plan.dec(str(bar[-1, 3]))), "0"] for k, bar in enumerate(bars)]
    return dict(symbol="BTCUSDT", venue="binance-usdm", candles=rows)


def sample(quote_path, day, trade, mark, *, include_plans=False):
    frame = pd.read_csv(quote_path)
    if not frame.symbol.eq("BTCUSDT").all() or not frame.exchange.eq("binance-futures").all():
        raise ValueError("Unexpected historical quote")
    rx = frame.local_timestamp.to_numpy()
    if np.any(np.diff(rx) < 0):
        raise ValueError("Historical receive time order")
    start = core.stamp(day)
    for closed in range(start, start + 24*HOUR, 4*HOUR):
        now = closed + 120000
        result = dict(time_ms=now)
        index = rx.searchsorted(now*1000, side="right") - 1
        if index < 0:
            yield dict(result, unavailable="quote_missing")
            continue
        q = frame.iloc[index]
        if not q.timestamp <= q.local_timestamp <= now*1000 or now*1000-q.timestamp>5_000_000:
            yield dict(result, unavailable="quote_age")
            continue
        cap = captures(trade, closed)
        if cap is None:
            yield dict(result, unavailable="candles_missing")
            continue
        mark_reference = mark[core.ix(now)-1, 3]
        vals = (q.bid_price, q.ask_price, q.bid_amount, q.ask_amount, mark_reference)
        if not np.isfinite(vals).all():
            yield dict(result, unavailable="quote_or_mark")
            continue
        book = dict(id=f"historical-{q.timestamp}", symbol="BTCUSDT", event_ms=int(q.timestamp)//1000,
                    received_ms=int(q.local_timestamp)//1000, **dict(zip(
                        ("bid", "ask", "bid_qty", "ask_qty", "mark"), (str(v) for v in vals))))
        filters = dict(as_of_ms=now, tick_size=".1", step_size=".001",
                       min_qty=".001", min_notional="50")
        news = dict(coverage="limited", high_risk_until_ms=0, items=[])
        before = plan.context(cap, book, filters, news, now)
        after = plan.context(cap, book, filters, news, now, candidate_policy=plan.TREND_ALIGNMENT_POLICY)
        result.update(before={side: sum(c["direction"] == side for c in before["candidate_plans"])
                              for side in ("LONG", "SHORT")},
                      after={side: sum(c["direction"] == side for c in after["candidate_plans"])
                             for side in ("LONG", "SHORT")}, alignment=after["trend_alignment"])
        if include_plans:
            result["candidates"] = after["candidate_plans"]
            i = rx.searchsorted((now + 60000)*1000, side="right") - 1
            arm = frame.iloc[i]
            if 0 <= (now + 60000)*1000-arm.timestamp <= 5_000_000 and arm.timestamp <= arm.local_timestamp:
                if 0 < arm.bid_price <= arm.ask_price:
                    result["arm_quote"] = dict(bid=str(arm.bid_price), ask=str(arm.ask_price),
                                               event_ms=int(arm.timestamp)//1000)
        yield result


def run(price, quotes, out):
    trade, mark, _, files, overlaps = core.load_data(price, "BTCUSDT")
    scheduled, hourly = history(trade)
    manifest = json.loads((quotes / "manifest.json").read_text())
    matches = []
    for item in manifest:
        path = quotes / item["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("Quote checksum mismatch")
        matches.extend(sample(path, item["day"], trade, mark))
    ready = [r for r in matches if "before" in r]
    slot_map = {r["time_ms"]:r["alignment"] for r in scheduled}
    if any(slot_map[r["time_ms"]] != r["alignment"] for r in ready):
        raise ValueError("Historical trend feature parity mismatch")
    out.mkdir(parents=True, exist_ok=True)
    result = dict(scheduled=counts(scheduled), hourly=counts(hourly),
                  off_schedule=counts([r for r in hourly if (r["time_ms"]-120000)%(4*HOUR)]),
                  by_year={str(y): counts([r for r in scheduled if core.stamp(f"{y}-01-01")
                                           <= r["time_ms"] < core.stamp(f"{y+1}-01-01")])
                           for y in (2025, 2026)},
                  sample=dict(n=len(matches), ready=len(ready),
                              before_nonempty=sum(any(r["before"].values()) for r in ready),
                              after_nonempty=sum(any(r["after"].values()) for r in ready),
                              after_trend_wait=sum(r["alignment"]["allowed"] is None for r in ready),
                              before_candidates=sum(sum(r["before"].values()) for r in ready),
                              after_candidates=sum(sum(r["after"].values()) for r in ready)),
                  archive_sources=files, overlaps=overlaps, quote_sources=manifest)
    (out/"summary.json").write_text(json.dumps(result, indent=2, allow_nan=False)+"\n")
    (out/"samples.json").write_text(json.dumps(matches, indent=2, allow_nan=False)+"\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("price", "quotes", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    run(args.price, args.quotes, args.output)
