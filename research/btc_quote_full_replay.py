"""Replay the frozen hourly-direction baseline with minute and quote execution."""
import argparse
from collections import Counter
import gzip
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from research import action_values as action
from research.btc_quote_archive import DAY, MINUTE, SECOND

core = action.core
FEE = .0005
LATENCY = SECOND // 10


class Quotes:
    def __init__(self, frame):
        if not frame.exchange.eq("binance-futures").all() or not frame.symbol.eq("BTCUSDT").all():
            raise ValueError("Unexpected quote instrument")
        self.rx = frame.local_timestamp.to_numpy()
        self.event = frame.timestamp.to_numpy()
        if not len(self.rx) or np.any(np.diff(self.rx) < 0):
            raise ValueError("Quote receive order")
        self.price = {1: frame.ask_price.to_numpy(), -1: frame.bid_price.to_numpy()}
        self.size = {1: frame.ask_amount.to_numpy(), -1: frame.bid_amount.to_numpy()}
        self.good = ((self.event <= self.rx) & (self.rx - self.event <= 5*SECOND)
                     & (self.price[-1] > 0) & (self.price[1] >= self.price[-1])
                     & np.isfinite(self.price[1]) & np.isfinite(self.size[1])
                     & np.isfinite(self.size[-1]) & (self.size[-1] > 0) & (self.size[1] > 0))

    def at(self, time, side, qty):
        i = self.rx.searchsorted(time, side="right") - 1
        if i < 0 or not self.good[i] or time - self.event[i] > 5*SECOND or self.size[side][i] < qty:
            return None
        return float(self.price[side][i])

    def trigger(self, start, end, side, stop, target):
        i, j = self.rx.searchsorted([start, end], side="left")
        px = self.price[-side][i:j]
        stopped = side*(px-stop) <= 0
        reached = side*(px-target) >= 0
        hit = np.flatnonzero(self.good[i:j] & (stopped | reached))
        if not len(hit):
            return end, "TIME"
        k = hit[0]
        return int(self.rx[i+k]), "SL" if stopped[k] else "TP"


def quote_trade(row, side, delay, quotes, mark, ft, fr, cash):
    opened = int(row.ts)*1000 + delay*MINUTE + LATENCY
    entry = quotes.at(opened, side, .001)
    if entry is None:
        return dict(reject="entry_quote")
    m0 = mark[core.ix(opened//MINUTE*core.M), 0]
    if not np.isfinite(m0):
        return dict(reject="entry_mark")
    qty, reason = core.size_for(entry, m0, "BTCUSDT", 0)
    if reason:
        return dict(reject=reason)
    if quotes.at(opened, side, qty) is None:
        return dict(reject="entry_quantity")
    atr = row.atr
    stop = math.floor((entry-atr)*10)/10 if side == 1 else math.ceil((entry+atr)*10)/10
    target = math.ceil((entry+2*atr)*10)/10 if side == 1 else math.floor((entry-2*atr)*10)/10
    if qty*entry*(1+FEE) > cash or qty*(abs(entry-stop)+.0031*entry) > 5:
        return dict(reject="capital_or_risk")
    deadline = opened + 240*MINUTE
    trigger, why = quotes.trigger(opened, deadline, side, stop, target)
    closed = trigger+LATENCY
    exit_price = quotes.at(closed, -side, qty)
    if exit_price is None:
        return dict(unresolved="exit_quote")
    funding = 0.
    for n in range(ft.searchsorted(opened, side="right"), ft.searchsorted(closed, side="left")):
        mp = mark[core.ix(int(ft[n]//MINUTE)*core.M), 0]
        if not np.isfinite(mp):
            return dict(unresolved="funding_mark")
        funding += -side*qty*mp*fr[n]
    fees = qty*(entry+exit_price)*FEE
    gross = side*qty*(exit_price-entry)
    return dict(entry_us=opened, exit_us=closed, trigger_us=trigger, reason=why, qty=qty,
                entry=entry, exit=exit_price, stop=stop, target=target, gross=gross, fees=fees,
                impact=0., funding=funding, net=gross-fees+funding)


def minute_trade(row, side, stress, trade, mark, ft_ms, fr, cash):
    path = action.path(int(row.at), row.atr, side, stress, trade, mark, ft_ms, fr, core.START)
    if path is None:
        return dict(reject="incomplete_minute_path")
    qty, reason = core.size_for(path["entry"], mark[path["i"], 0], "BTCUSDT", 0)
    if reason:
        return dict(reject=reason)
    impact = .001 if stress else .0005
    if qty*path["entry"]*(1+FEE) > cash or qty*(abs(path["entry"]-path["stop"])
                                               +path["entry"]*(.0031+2*impact)) > 5:
        return dict(reject="capital_or_risk")
    components = {name: qty*path[name] for name in ("gross", "fees", "impact", "funding", "net")}
    return dict(entry_us=core.START*1000+path["i"]*MINUTE,
                exit_us=core.START*1000+path["until"]*MINUTE, reason=path["why"],
                qty=qty, entry=path["entry"], exit=path["exit"], **components)


def replay(rows, account, execute):
    records = []
    for row in rows.itertuples():
        item = dict(signal_ms=int(row.ts), side=int(np.sign(row.r60)))
        if not item["side"] or row.ts*1000 < account["until"] or account["unresolved"]:
            continue
        result = execute(row, item["side"], account["cash"])
        if "unresolved" in result:
            account["unresolved"] = result["unresolved"]
        if "net" in result:
            account["until"] = result["exit_us"]
            account["cash"] += result["net"]
        records.append(dict(item, **result))
    return records


def summarize(rows, account):
    trades = [x for x in rows if "net" in x]
    components = {name: sum(v[name] for v in trades) for name in ("gross", "fees", "impact", "funding", "net")}
    if abs(account["cash"]-1000-components["net"]) > 1e-8:
        raise ValueError("Balance mismatch")
    return dict(components, n=len(trades), without_best=components["net"]-max((r["net"] for r in trades), default=0),
                exits=Counter(r["reason"] for r in trades), rejects=Counter(r["reject"] for r in rows if "reject" in r),
                unresolved=account["unresolved"])


def run(archive, raw, signals, output):
    trade, mark, funding, files, overlaps = core.load_data(raw, "BTCUSDT")
    ft_ms, fr = map(np.asarray, zip(*funding))
    ft = ft_ms*1000
    predictions = pd.read_csv(signals)
    if not (predictions.ts == core.START + predictions["at"]*core.M).all():
        raise ValueError("Signal indices differ from the archive")
    output.mkdir(parents=True, exist_ok=True)
    keys = ("minute_base", "quote_base", "minute_stress", "quote_stress")
    accounts = {key: dict(cash=1000., until=-1, unresolved=None) for key in keys}
    records = {key: [] for key in keys}
    days = {}
    manifest = json.loads((archive / "manifest.json").read_text())
    for item in manifest:
        path = archive / item["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("Quote hash mismatch")
        quotes = Quotes(pd.read_csv(path))
        start = pd.Timestamp(item["day"], tz="UTC").value//1000
        rows = predictions.loc[(predictions.ts*1000 >= start)
                               & (predictions.ts*1000 + 246*MINUTE + SECOND < start+DAY)]
        days[item["day"]] = {}
        for name, stress, delay in (("base", False, 1), ("stress", True, 6)):
            def minute_path(r, side, cash):
                return minute_trade(r, side, stress, trade, mark, ft_ms, fr, cash)

            def quote_path(r, side, cash):
                return quote_trade(r, side, delay, quotes, mark, ft, fr, cash)

            for prefix, execute in (("minute", minute_path), ("quote", quote_path)):
                key = f"{prefix}_{name}"
                day_rows = replay(rows, accounts[key], execute)
                records[key].extend(day_rows)
                days[item["day"]][key] = dict(n=sum("net" in r for r in day_rows),
                                              net=sum(r.get("net", 0) for r in day_rows))
    totals = {key: summarize(records[key], accounts[key]) for key in keys}
    matches = {}
    for name in ("base", "stress"):
        minute = {r["signal_ms"]: r for r in records[f"minute_{name}"] if "net" in r}
        quote = {r["signal_ms"]: r for r in records[f"quote_{name}"] if "net" in r}
        shared = minute.keys() & quote.keys()
        matches[name] = dict(shared=len(shared),
                             minute_only=len(minute.keys() - quote.keys()),
                             quote_only=len(quote.keys() - minute.keys()),
                             shared_net_change=sum(quote[t]["net"]-minute[t]["net"] for t in shared),
                             shared_reason_changed=sum(quote[t]["reason"]!=minute[t]["reason"] for t in shared))
    summary = dict(totals=totals, days=days, source_manifest=manifest, price_sources=files,
                   overlaps=overlaps, signal_sha256=hashlib.sha256(signals.read_bytes()).hexdigest(),
                   matches=matches)
    (output/"summary.json").write_text(json.dumps(summary,indent=2,allow_nan=False)+"\n")
    (output/"replays.json.gz").write_bytes(gzip.compress(json.dumps(records,allow_nan=False).encode(),mtime=0))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("quotes", "minutes", "signals", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    run(args.quotes, args.minutes, args.signals, args.output)
