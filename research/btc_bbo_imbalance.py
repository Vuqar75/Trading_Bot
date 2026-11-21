"""Offline test of persistent BTC best-quote imbalance."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.btc_quote_archive import DAY, MINUTE, SECOND
from research.btc_quote_full_replay import FEE, Quotes, core, summarize

HOLD = 15 * MINUTE
STOP = .003
HORIZONS = {"base": (SECOND//10, 0.), "stress": (SECOND, .0001)}
THRESHOLD = .35


def imbalance(quotes, at):
    sample = at + np.arange(-59, 1) * SECOND
    idx = quotes.rx.searchsorted(sample, side="right") - 1
    safe = np.maximum(0, idx)
    valid = (idx >= 0) & quotes.good[safe] & (sample-quotes.event[safe] <= 5*SECOND)
    if valid.sum() < 55:
        return None
    bid, ask = (quotes.size[side][safe[valid]] for side in (-1, 1))
    return float(np.mean((bid-ask)/(bid+ask)))


def trade(quotes, at, side, score, delay, impact, mark, ft, fr, account):
    row = dict(signal_us=at, side=side, imbalance=score)
    entry_time = at + delay
    if entry_time < account["until"]:
        return dict(row, reject="occupied")
    raw_entry = quotes.at(entry_time, side, .001)
    if raw_entry is None:
        return dict(row, reject="entry_quote")
    entry = raw_entry * (1 + side*impact)
    mark_entry = mark[core.ix(entry_time//MINUTE*core.M), 0]
    if not np.isfinite(mark_entry):
        return dict(row, reject="entry_mark")
    if not 50 <= .001*max(entry, mark_entry) <= 100 or .001*mark_entry < 50:
        return dict(row, reject="notional")
    if .001*entry*(STOP+.0031+2*impact) > 5 or .001*entry*(1+FEE) > account["cash"]:
        return dict(row, reject="capital_or_risk")
    stop = entry * (1-side*STOP)
    deadline = entry_time+HOLD
    # An infinite target disables TP for the chosen side.
    trigger, reason = quotes.trigger(entry_time, deadline, side, stop, side*np.inf)
    exit_time = trigger + delay
    raw_exit = quotes.at(exit_time, -side, .001)
    if raw_exit is None:
        account["unresolved"] = "exit_quote"
        return dict(row, unresolved="exit_quote")
    exit_price = raw_exit * (1-side*impact)
    funding = 0.
    for k in range(ft.searchsorted(entry_time, side="right"), ft.searchsorted(exit_time, side="left")):
        m = mark[core.ix(int(ft[k]//MINUTE)*core.M), 0]
        if not np.isfinite(m):
            account["unresolved"] = "funding_mark"
            return dict(row, unresolved="funding_mark")
        funding += float(-side*.001*m*fr[k])
    gross = .001*side*(raw_exit-raw_entry)
    fees = .001*(entry+exit_price)*FEE
    costs = .001*(abs(entry-raw_entry)+abs(exit_price-raw_exit))
    net = gross-fees-costs+funding
    account["cash"] += net
    account["until"] = exit_time
    return dict(row, entry_us=entry_time, exit_us=exit_time, reason=reason, qty=.001,
                entry=entry, exit=exit_price, stop=stop, gross=gross, fees=fees, impact=costs,
                net=net, funding=funding)


def run(archive, raw, output):
    _, mark, rates, files, overlaps = core.load_data(raw, "BTCUSDT")
    ft_ms, fr = map(np.asarray, zip(*rates))
    ft = ft_ms * 1000
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((archive/"manifest.json").read_text())
    accounts = {f"{name}_{direction}": dict(cash=1000., until=-1, unresolved=None)
                for name in HORIZONS for direction in ("follow", "opposite")}
    journals = {key: [] for key in accounts}
    stats = {}
    for item in manifest:
        path = archive/item["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("Quote hash mismatch")
        quotes = Quotes(pd.read_csv(path))
        start = pd.Timestamp(item["day"], tz="UTC").value//1000
        points = range(start, start+DAY-HOLD-2*SECOND, HOLD)
        stats[item["day"]] = dict(evaluable=0, signals=0, skips_before_model=0,
                                  cases={key: 0. for key in accounts})
        day = stats[item["day"]]
        for at in points:
            score = imbalance(quotes, at)
            if score is None:
                day["skips_before_model"] += 1
                continue
            day["evaluable"] += 1
            if abs(score) < THRESHOLD:
                continue
            day["signals"] += 1
            for name, (delay, impact) in HORIZONS.items():
                for policy, multiplier in (("follow", 1), ("opposite", -1)):
                    key = f"{name}_{policy}"
                    if accounts[key]["unresolved"]:
                        continue
                    result = trade(quotes, at, multiplier*(1 if score > 0 else -1), score,
                                   delay, impact, mark, ft, fr, accounts[key])
                    journals[key].append(dict(result, day=item["day"]))
                    day["cases"][key] += result.get("net", 0)
    results = {}
    for key, rows in journals.items():
        data = summarize(rows, accounts[key])
        data["positive_days"] = sum(1 for d in stats.values() if d["cases"][key]>0)
        data["without_best_day"] = data["net"] - max(d["cases"][key] for d in stats.values())
        data["sides"] = {str(side): dict(n=sum(r["side"] == side and "net" in r for r in rows),
                                       net=sum(r.get("net", 0) for r in rows if r["side"] == side))
                         for side in (1, -1)}
        results[key] = data
    (output/"summary.json").write_text(json.dumps(dict(results=results, days=stats,
        quote_sources=manifest, price_sources=files, overlaps=overlaps), indent=2, allow_nan=False)+"\n")
    (output/"trades.json").write_text(json.dumps(journals, indent=2, allow_nan=False)+"\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("quotes", "minutes", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    run(args.quotes, args.minutes, args.output)
