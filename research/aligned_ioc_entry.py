"""Same-signal offline comparison of passive and bounded immediate BTC entry."""
import argparse
from collections import Counter
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research import aligned_entry_reach as reach, trend_alignment_coverage as coverage

core = coverage.core
policy = coverage.plan
HOUR = coverage.HOUR
HOLD = HOUR
QTY = .001
FEE = .0005
BUFFER = .0021
STOP_SLIP = .001
MILLIS_PER_DAY = 24 * HOUR


class Quotes:
    def __init__(self, rows):
        if not rows.symbol.eq("BTCUSDT").all() or not rows.exchange.eq("binance-futures").all():
            raise ValueError("Unexpected contract")
        self.time = rows.local_timestamp.to_numpy()
        self.event = rows.timestamp.to_numpy()
        if not len(self.time) or np.any(np.diff(self.time) < 0):
            raise ValueError("Quotes out of order")
        self.price = {1: rows.ask_price.to_numpy(), -1: rows.bid_price.to_numpy()}
        self.qty = {1: rows.ask_amount.to_numpy(), -1: rows.bid_amount.to_numpy()}
        self.valid = ((self.event <= self.time) & (self.time-self.event <= 5_000_000)
                      & (self.price[-1] > 0) & (self.price[1] >= self.price[-1])
                      & np.isfinite(self.price[1]) & np.isfinite(self.qty[1])
                      & np.isfinite(self.qty[-1]) & (self.qty[-1] > 0) & (self.qty[1] > 0))

    def at(self, time):
        index = self.time.searchsorted(time*1000, side="right") - 1
        if index < 0 or not self.valid[index] or time*1000-self.event[index]>5_000_000:
            return None
        return dict(bid=float(self.price[-1][index]), ask=float(self.price[1][index]),
                    bid_qty=float(self.qty[-1][index]), ask_qty=float(self.qty[1][index]),
                    event_ms=int(self.event[index]//1000), received_ms=int(self.time[index]//1000))

    def exit_trigger(self, entry_ms, deadline, side, stop, target):
        i, j = self.time.searchsorted([entry_ms*1000, deadline*1000], side="left")
        prices = self.price[-side][i:j]
        stops = side*(prices-stop)<=0
        hits = np.flatnonzero(self.valid[i:j] & (stops | (side*(prices-target)>=0)))
        if not len(hits):
            return deadline, "TIME"
        number = hits[0]
        time = int((self.time[i+number]+999)//1000)
        return min(time, deadline), "SL" if stops[number] else "TP"


def immediate_price(side, entry, stop, target, impact):
    raw = Decimal(str(entry)) * (1+side*Decimal(str(impact)))
    tick = Decimal(".1")
    price = (raw/tick).to_integral_value(rounding=ROUND_CEILING if side==1 else ROUND_FLOOR)*tick
    stop, target = Decimal(str(stop)), Decimal(str(target))
    fee, buffer, qty = Decimal(str(FEE)), Decimal(str(BUFFER)), Decimal(str(QTY))
    stop_fill = stop*(1-side*Decimal(str(STOP_SLIP)))
    loss = side*(price-stop_fill) + fee*(price+stop_fill) + price*buffer
    reward = side*(target-price) - fee*(price+target) - price*buffer
    if not (side*(price-stop)>0 and side*(target-price)>0 and loss>0
            and reward>=Decimal("1.5")*loss and qty*loss<=5 and 50<=qty*price<=100):
        return None
    return float(price)


def setups(day, quotes, trade, mark, fast, slow):
    signals = []
    counts = Counter()
    start = core.stamp(day)
    for closed in range(start, start + MILLIS_PER_DAY-HOLD-policy.BAR-1000, policy.BAR):
        as_of = closed+120000
        hour_close = closed//HOUR*HOUR
        four_close = closed//(4*HOUR)*(4*HOUR)
        if hour_close not in fast or four_close not in slow:
            counts["trend_unavailable"] += 1
            continue
        trend = policy.trend_alignment({"1h":fast[hour_close], "4h":slow[four_close]}, as_of)
        side = trend["allowed"]
        if side is None:
            counts["trend_wait"] += 1
            continue
        i = core.ix(closed)
        previous = trade[i-30:i-15]
        if not (trade[i-1, 3]>previous[:,1].max() if side=="LONG"
                else trade[i-1, 3]<previous[:,2].min()):
            counts["no_15m_confirmation"] += 1
            continue
        q = quotes.at(as_of)
        capture = coverage.captures(trade, closed)
        mid = mark[core.ix(as_of)-1, 3]
        if q is None or capture is None or not np.isfinite(mid):
            counts["context_unavailable"] += 1
            continue
        quote = dict(q, id=f"historical-{as_of}", symbol="BTCUSDT", mark=str(mid))
        for key in ("ask","bid","ask_qty","bid_qty"):
            quote[key] = str(quote[key])
        filters = dict(as_of_ms=as_of,tick_size=".1",step_size=".001",min_qty=".001",min_notional="50")
        news = dict(coverage="limited",high_risk_until_ms=0,items=[])
        snapshot = policy.context(capture, quote, filters, news, as_of,
                                  candidate_policy=policy.TREND_ALIGNMENT_POLICY)
        candidates = snapshot["candidate_plans"]
        if not candidates:
            counts["no_geometry"] += 1
            continue
        selected = candidates[0]
        signals.append(dict(selected, at_ms=as_of, deadline_ms=snapshot["expires_ms"]))
    counts["setups"] = len(signals)
    return signals, counts


def entry(signal, side, quotes, tape, arm, is_ioc, impact):
    quote = quotes.at(arm)
    if quote is None:
        return dict(reject="arm_quote")
    side_name = "ask" if side==1 else "bid"
    if is_ioc:
        stop, target = float(signal["stop"]), float(signal["target"])
        if quote[side_name+"_qty"] < QTY:
            return dict(reject="visible_quantity")
        px = immediate_price(side, quote[side_name], stop, target, impact)
        if px is None:
            return dict(reject="price_beyond_net_rr_or_limits")
        return dict(entry=px, entry_ms=arm)
    px = float(signal["entry"])
    if side*(px-quote[side_name])>=0:
        return dict(reject="limit_would_take")
    rows = reach.archive_window(tape, arm, signal["deadline_ms"])
    if rows is None:
        return dict(unresolved="entry_trade_window")
    matches = rows.loc[((rows.price<px) if side==1 else (rows.price>px))
                       & rows.is_buyer_maker.eq(side==1) & rows.quantity.ge(QTY)]
    if matches.empty:
        return dict(reject="expired", occupied_to=signal["deadline_ms"])
    hit = matches.iloc[0]
    return dict(entry=px, entry_ms=int(hit.transact_time), entry_trade_id=int(hit.agg_trade_id))


def finish(opened, side, stop, target, quotes, mark, funding, delay, impact):
    timestamp = opened["entry_ms"]
    trigger, reason = quotes.exit_trigger(timestamp, timestamp+HOLD, side, stop, target)
    closed = trigger+delay
    q = quotes.at(closed)
    field = "bid" if side==1 else "ask"
    if q is None or q[field+"_qty"]<QTY:
        return dict(opened, unresolved="exit_quote")
    price = q[field]*(1-side*(impact+(STOP_SLIP if reason=="SL" else 0)))
    applied = 0.
    for stamp, rate in funding:
        if timestamp < stamp < closed:
            mp = mark[core.ix(stamp-stamp%core.M),0]
            if not np.isfinite(mp):
                return dict(opened, unresolved="funding_mark")
            applied += float(-side*QTY*mp*rate)
    gross = QTY*side*(price-opened["entry"])
    fees = QTY*(opened["entry"]+price)*FEE
    return dict(opened, exit_ms=closed, exit=price, reason=reason, gross=gross, fees=fees,
                funding=applied, net=gross-fees+applied)


def play(signals, quotes, tape, mark, funding, account, is_ioc, stress):
    arm_delay, exit_delay, impact = (90000,1000,.0001) if stress else (60000,100,0.)
    records = []
    for signal in signals:
        if account["unresolved"]:
            break
        side = 1 if signal["direction"]=="LONG" else -1
        arm = signal["at_ms"]+arm_delay
        if signal["at_ms"] < account["busy_until"]:
            result = dict(reject="occupied")
        else:
            result = entry(signal, side, quotes, tape, arm, is_ioc, impact)
        if "entry" in result:
            if QTY*result["entry"]*(1+FEE)+QTY*result["entry"]*BUFFER > account["cash"]:
                result = dict(reject="cash")
            else:
                result = finish(result,side,float(signal["stop"]),float(signal["target"]),
                                quotes,mark,funding,exit_delay,impact)
        if "unresolved" in result:
            account["unresolved"] = result["unresolved"]
        if "net" in result:
            account["cash"] += result["net"]
        if "exit_ms" in result or "occupied_to" in result:
            account["busy_until"] = result.get("exit_ms", result.get("occupied_to"))
        records.append(dict(signal, **result))
    return records


def summarize(rows, account):
    trades = [r for r in rows if "net" in r]
    totals = {key:sum(r[key] for r in trades) for key in ("gross","fees","funding","net")}
    if abs(account["cash"]-1000-totals["net"]) > 1e-8:
        raise ValueError("Ledger mismatch")
    return dict(totals,n=len(trades),unresolved=account["unresolved"],
                rejects=Counter(r["reject"] for r in rows if "reject" in r),
                exits=Counter(r["reason"] for r in trades))


def run(prices, quotes, trades, out):
    trade, mark, funding, sources, overlaps = core.load_data(prices,"BTCUSDT")
    fast, slow = (coverage.trend_frames(trade,h) for h in (HOUR,4*HOUR))
    qm = {r["day"]:r for r in json.loads((quotes/"manifest.json").read_text())}
    tm = json.loads((trades/"manifest.json").read_text())
    keys = [(p, s) for p in ("passive","ioc") for s in ("base","stress")]
    accounts = {k:dict(cash=1000.,busy_until=0,unresolved=None) for k in keys}
    ledger = {k:[] for k in keys}
    days = {}
    for item in tm:
        day = item["day"]
        qp, tp = quotes/qm[day]["file"], trades/item["file"]
        for path, manifest in ((qp,qm[day]),(tp,item)):
            if hashlib.sha256(path.read_bytes()).hexdigest()!=manifest["sha256"]:
                raise ValueError("Source hash mismatch")
        quote_data = Quotes(pd.read_csv(qp))
        tape = pd.read_csv(tp,usecols=["agg_trade_id","transact_time","price","quantity","is_buyer_maker"])
        if not tape.transact_time.is_monotonic_increasing or not pd.api.types.is_bool_dtype(tape.is_buyer_maker):
            raise ValueError("Unusable trade archive")
        signals, counts = setups(day,quote_data,trade,mark,fast,slow)
        days[day] = counts
        for (key, scenario), account in accounts.items():
            ledger[key,scenario].extend(play(signals,quote_data,tape,mark,funding,account,
                                             key=="ioc",scenario=="stress"))
    cases = {f"{p}_{s}":summarize(ledger[p,s],accounts[p,s]) for p,s in keys}
    attribution = {}
    for s in ("base","stress"):
        p = {r["at_ms"]:r["net"] for r in ledger["passive",s] if "net" in r}
        q = {r["at_ms"]:r["net"] for r in ledger["ioc",s] if "net" in r}
        shared, added, removed = p.keys() & q.keys(), q.keys()-p.keys(), p.keys()-q.keys()
        attribution[s] = dict(shared=len(shared),shared_delta=sum(q[t]-p[t] for t in shared),
                              ioc_only=len(added),ioc_only_net=sum(q[t] for t in added),
                              passive_only=len(removed),passive_only_net=sum(p[t] for t in removed))
    out.mkdir(parents=True,exist_ok=True)
    summary = dict(cases=cases,attribution=attribution,days=days,price_sources=sources,
                   price_overlaps=overlaps,trade_sources=tm,quote_sources=[qm[r["day"]] for r in tm])
    (out/"summary.json").write_text(json.dumps(summary,indent=2,allow_nan=False)+"\n")
    (out/"journals.json").write_text(json.dumps({f"{p}_{s}":ledger[p,s] for p,s in keys},
                                               indent=2,allow_nan=False)+"\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for k in ("prices","quotes","trades","output"):
        parser.add_argument(k,type=Path)
    args = parser.parse_args()
    run(args.prices,args.quotes,args.trades,args.output)
