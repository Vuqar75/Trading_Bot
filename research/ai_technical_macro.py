"""Matched offline evaluation of technical, derivative and macro features."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from research import action_values
from research.derivative_metrics_data import load as load_metrics
from research.oi_response import asof

core = action_values.core
M = core.M
H = 60 * M
DAY = 24 * H
TRAIN = core.stamp("2025-07-01")
TEST = core.stamp("2026-01-01")
MID = core.stamp("2026-07-01")
FED = Path(__file__).with_name("AI_TECHNICAL_MACRO_FED.csv")
TECH = ["r1", "r4", "r24", "range_position", "atr_pct"]
DERIV = ["oi_1h", "oi_24h", "top_positions", "taker_ratio", "funding", "funding_age"]
MACRO = ["fed_target", "fed_change", "fed_r1", "fed_r4", "fed_r24"]
MODELS = {"technical": TECH, "derivatives": TECH + DERIV, "macro": TECH + DERIV + MACRO}


def features(trade, funding, derivatives, fed):
    bars = trade.reshape(-1, 60, 4)
    close = bars[:, -1, 3]
    high = bars[:, :, 1].max(axis=1)
    low = bars[:, :, 2].min(axis=1)
    prev = np.r_[np.nan, close[:-1]]
    tr = np.maximum.reduce([high - low, abs(high - prev), abs(low - prev)])
    time = core.START + np.arange(1, len(close) + 1) * H
    data = pd.DataFrame(dict(time=time, atr=pd.Series(tr).rolling(14).mean()))
    for n in (1, 4, 24):
        data[f"r{n}"] = pd.Series(close).pct_change(periods=n, fill_method=None)
    hi = pd.Series(high).rolling(24).max()
    lo = pd.Series(low).rolling(24).min()
    with np.errstate(divide="ignore", invalid="ignore"):
        data["range_position"] = (close - lo) / (hi - lo)
        data["atr_pct"] = data.atr / close
    metric_time, values = derivatives
    query = time - 15 * M
    oi, _, oi_time = asof(metric_time, values[:, 0], query)
    for n in (1, 24):
        before, _, _ = asof(metric_time, values[:, 0], query - n * H)
        data[f"oi_{n}h"] = oi / before - 1
    for name, col in (("top_positions", 2), ("taker_ratio", 4)):
        value, _, _ = asof(metric_time, values[:, col], query)
        data[name] = np.log(value)
    ft, fr = map(np.asarray, zip(*funding))
    idx = np.searchsorted(ft, query, side="right") - 1
    data["funding"] = np.where(idx >= 0, fr[idx], np.nan)
    data["funding_age"] = np.where(idx >= 0, (time - ft[idx]) / H, np.nan)
    dates = pd.to_datetime(time // DAY * DAY, unit="ms")
    target = fed.reindex(dates - pd.Timedelta(days=2)).to_numpy()
    previous = fed.reindex(dates - pd.Timedelta(days=32)).to_numpy()
    data["fed_target"] = target
    data["fed_change"] = target - previous
    for n in (1, 4, 24):
        data[f"fed_r{n}"] = target * data[f"r{n}"]
    data["oi_ms"] = oi_time
    data["funding_ms"] = np.where(idx >= 0, ft[idx], -1)
    data["fed_known_day_ms"] = time // DAY * DAY - 2 * DAY
    data["at"] = (time - core.START) // M
    complete = np.isfinite(data[TECH + DERIV + MACRO + ["atr"]]).all(axis=1)
    return data.loc[complete & (data.atr > 0) & (data.time >= TRAIN)].copy()


def simulated_path(row, side, stress, market):
    return action_values.path(int(row.at), row.atr, side, stress, *market, core.START)


def fit_forecasts(data, market):
    train = data.loc[data.time + 246 * M < TEST].copy()
    labels = []
    ready = []
    for row in train.itertuples():
        outcomes = [simulated_path(row, side, True, market) for side in (1, -1)]
        ready.append(all(item is not None for item in outcomes))
        if ready[-1]:
            labels.append([item["bp"] for item in outcomes])
    if not labels:
        raise ValueError("No complete training labels")
    train = train.loc[ready]
    test = data.loc[(data.time >= TEST) & (data["at"] + 246 < len(market[0]))].copy()
    for name, columns in MODELS.items():
        model = make_pipeline(StandardScaler(), Ridge(alpha=100))
        model.fit(train[columns], labels)
        long, short = model.predict(test[columns]).T
        test[f"{name}_long"] = long
        test[f"{name}_short"] = short
        test[name] = np.where((long > short) & (long > 5), 1,
                              np.where((short > long) & (short > 5), -1, 0))
    test["rule"] = np.sign(test.r1).astype(int)
    return test, len(train), int(train.time.max() + 246 * M)


def replay(test, market, variant, stress):
    account = dict(cash=1000., peak=1000., max_dd=0.)
    busy = -1
    fills = []
    counts = Counter()
    mark = market[1]
    impact = .001 if stress else .0005
    for row in test.itertuples():
        side = int(getattr(row, variant))
        if not side or row.at < busy:
            counts["wait" if not side else "busy"] += 1
            continue
        path = simulated_path(row, side, stress, market)
        if path is None:
            counts["incomplete_path"] += 1
            continue
        qty, reason = core.size_for(path["entry"], mark[path["i"], 0], "BTCUSDT", impact)
        if reason:
            counts[reason] += 1
            continue
        if qty * path["entry"] * 1.0005 > account["cash"]:
            counts["cash"] += 1
            continue
        risk = qty * (abs(path["entry"] - path["stop"]) + path["entry"] * (.0031 + 2 * impact))
        if risk > 5:
            counts["risk"] += 1
            continue
        busy = path["until"]
        for j in range(path["i"], path["j"] + 1):
            o, h, l, _ = mark[j]
            prices = (o,) if j == path["j"] and path["why"] == "TIME" else (
                (h, l) if side == 1 else (l, h)
            )
            for price in prices:
                delta = (side * (price - path["entry"]) - path["entry"] * .0005
                         - price * (impact + .0005) + min(0, path["funding"]))
                core.equity_update(account, account["cash"] + qty * delta)
        account["cash"] += qty * path["net"]
        core.equity_update(account, account["cash"])
        fills.append(dict(path, time=int(row.time), side=side, qty=qty, balance=account["cash"]))
    return fills, account, counts


def totals(fills):
    amounts = {key: sum(row["qty"] * row[key] for row in fills)
               for key in ("gross", "fees", "impact", "funding", "net")}
    if abs(amounts["gross"] - amounts["fees"] - amounts["impact"] +
           amounts["funding"] - amounts["net"]) > 1e-7:
        raise ValueError("PnL mismatch")
    return dict(amounts, n=len(fills), without_best=amounts["net"] -
                max((row["qty"] * row["net"] for row in fills), default=0))



def ranking_diagnostics(test, market):
    """Measure ranking without changing the trading threshold."""
    report = {}
    for side, label in ((1, "long"), (-1, "short")):
        paths = [simulated_path(row, side, True, market) for row in test.itertuples()]
        realized = pd.Series([path["bp"] if path is not None else np.nan for path in paths],
                             index=test.index)
        report[label] = {}
        for variant in MODELS:
            scores = test[f"{variant}_{label}"]
            paired = pd.DataFrame(dict(score=scores, realized=realized)).dropna()
            highest = paired.nlargest(max(1, len(paired) // 10), "score")
            report[label][variant] = dict(
                n=len(paired), spearman=paired.score.rank().corr(paired.realized.rank()),
                mean_bp=paired.realized.mean(), top_decile_mean_bp=highest.realized.mean()
            )
    return report


def run(archives, metrics_root, out):
    out.mkdir(parents=True, exist_ok=True)
    p, m, funding, sources, overlap = core.load_data(archives, "BTCUSDT")
    derivatives, quality = load_metrics(metrics_root)
    fed = pd.read_csv(FED, index_col=0, parse_dates=True)["DFEDTARL"]
    data = features(p, funding, derivatives["BTCUSDT"], fed)
    if not (data.oi_ms.le(data.time - 15 * M) & data.funding_ms.le(data.time - 15 * M)
            & data.fed_known_day_ms.le(data.time - 2 * DAY)).all():
        raise ValueError("Source latency mismatch")
    ft, fr = map(np.asarray, zip(*funding))
    market = p, m, ft, fr
    test, n, last_label = fit_forecasts(data, market)
    if last_label >= TEST:
        raise ValueError("Training crosses the test boundary")
    test.to_csv(out / "predictions.csv.gz", index=False, compression={"method": "gzip", "mtime": 0})
    summary = dict(train_count=n, latest_label_deadline=last_label, decision_count=len(test),
                   price_sources=sources, price_overlaps=overlap, metrics_quality=quality["BTCUSDT"],
                   metrics_manifest_sha256=hashlib.sha256((metrics_root / "manifest.json").read_bytes()).hexdigest(),
                   macro_sha256=hashlib.sha256(FED.read_bytes()).hexdigest(),
                   ranking=ranking_diagnostics(test, market), cases={})
    for variant in ("rule", *MODELS):
        for scenario, stress in (("base", False), ("stress", True)):
            fills, account, counts = replay(test, market, variant, stress)
            result = totals(fills)
            if abs(account["cash"] - 1000 - result["net"]) > 1e-7:
                raise ValueError("Balance mismatch")
            result.update(drawdown=account["max_dd"], counts=counts,
                          halves=[totals([r for r in fills if (r["time"] < MID) == first])
                                  for first in (True, False)],
                          sides={side: totals([r for r in fills if r["side"] == side])
                                 for side in (1, -1)})
            key = f"{variant}_{scenario}"
            summary["cases"][key] = result
            pd.DataFrame(fills).to_csv(out / f"{key}.csv.gz", index=False,
                                       compression={"method": "gzip", "mtime": 0})
    (out / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive", "metrics", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    run(args.archive, args.metrics, args.output)
