"""One frozen v20 signal / protected-paper execution check; no live orders.

Read RSI15M_VERIFICATION_PROTOCOL.md. Foreign Pine is not executed. Reuse only
the existing local research Account class without importing its unrelated ML
training dependency. No shared module or production setting is changed.
"""
import ast
import datetime as dt
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys
from collections import Counter
from zoneinfo import ZoneInfo

import numpy as np
from research import extreme_reversal as core

M = 60000
H = 60 * M
START = core.stamp("2024-12-01")
BEGIN = core.stamp("2025-01-01")
END = core.stamp("2026-01-01")
FOMC = {20250129, 20250319, 20250507, 20250618, 20250730, 20250917, 20251029, 20251210}
NY = ZoneInfo("America/New_York")


def configure_period():
    # This is an isolated research process; no DB or service is imported.
    core.START, core.BEGIN, core.END = START, BEGIN, END
    core.N = (END - START) // M
    core.SEGMENTS = [("Q" + str(q), core.stamp(f"2025-{3*q-2:02d}-01"),
                      core.stamp(f"2025-{3*q+1:02d}-01") if q < 4 else END)
                     for q in range(1, 5)]


def load_account_class():
    path = Path(__file__).with_name("published_ema_transfer.py")
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Account")
    namespace = {"np": np, "math": math, "core": core, "M": M, "H": H, "END": END, "Counter": Counter}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["Account"], hashlib.sha256(path.read_bytes()).hexdigest()


def rma(x, n):
    out = np.full(len(x), np.nan)
    valid = np.flatnonzero(np.isfinite(x))
    if len(valid) < n:
        return out
    start = int(valid[n - 1])
    assert np.isfinite(x[start-n+1:start+1]).all()
    out[start] = np.mean(x[start-n+1:start+1])
    for i in range(start + 1, len(x)):
        out[i] = (out[i-1] * (n-1) + x[i]) / n
    return out


def macro(t):
    x = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).astimezone(NY)
    key = x.year * 10000 + x.month * 100 + x.day
    return (key in FOMC and 12 <= x.hour <= 17) or (x.weekday() == 4 and x.day <= 7 and 6 <= x.hour <= 11)


def signals(trade):
    a = trade.reshape(-1, 15, 4)
    assert np.isfinite(a).all()
    o, h, l, c = a[:, 0, 0], a[:, :, 1].max(axis=1), a[:, :, 2].min(axis=1), a[:, -1, 3]
    change = np.r_[np.nan, np.diff(c)]
    up = rma(np.maximum(change, 0), 14)
    down = rma(np.maximum(-change, 0), 14)
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = 100 - 100 / (1 + up / down)
    ema = np.empty(len(c)); ema[0] = c[0]
    for i in range(1, len(c)):
        ema[i] = ema[i-1] + 2 / 201 * (c[i] - ema[i-1])
    prev = np.r_[c[0], c[:-1]]
    tr = np.maximum(h-l, np.maximum(np.abs(h-prev), np.abs(l-prev)))
    atr = rma(tr, 14) / c * 100
    body = np.divide(np.abs(c-o), h-l, out=np.zeros(len(c)), where=h>l)
    phases = {1: [0, -1, None], -1: [0, -1, None]}
    out = []
    for i in range(len(c)):
        raw = {1: i > 0 and rsi[i] > 82 and rsi[i-1] <= 82,
               -1: i > 0 and rsi[i] < 20 and rsi[i-1] >= 20}
        quality = bool(.2 <= atr[i] <= 1 and body[i] >= .3)
        base = {1: bool(raw[1] and c[i] > o[i] and c[i] > ema[i]*1.002
                        and quality and (c[i]/ema[i]-1)*100 >= 1.5),
                -1: bool(raw[-1] and c[i] < o[i] and c[i] < ema[i]*.998 and quality)}
        pb = {1: False, -1: False}
        for side in (1, -1):
            intact = c[i] > ema[i] if side == 1 else c[i] < ema[i]
            phase, watch, peak = phases[side]
            if raw[side] and intact:
                phase, watch, peak = 1, i+28, float(h[i] if side == 1 else l[i])
            elif phase:
                if i > watch or not intact:
                    phase = 0
                elif phase == 1:
                    peak = max(peak, h[i]) if side == 1 else min(peak, l[i])
                    retrace = (peak-c[i])/peak*100 if side == 1 else (c[i]-peak)/peak*100
                    if retrace >= 1:
                        phase = 2
                elif phase == 2:
                    resumed = c[i] > peak and c[i] > o[i] if side == 1 else c[i] < peak and c[i] < o[i]
                    if resumed and quality:
                        pb[side] = True
                        phase = 0
            phases[side] = [phase, watch, peak]
        side = 1 if base[1] or pb[1] else -1 if base[-1] or pb[-1] else 0
        assert not ((base[1] or pb[1]) and (base[-1] or pb[-1]))
        bar_time = START + i*15*M
        out.append({"bar_index": i, "time_ms": bar_time+15*M, "side": side,
                    "base": bool(base[side]) if side else False,
                    "pullback": bool(pb[side]) if side else False,
                    "rsi": None if not np.isfinite(rsi[i]) else float(rsi[i]),
                    "atr_pct": None if not np.isfinite(atr[i]) else float(atr[i]),
                    "close": float(c[i]), "ema200": float(ema[i]),
                    "tp_pct": .0215 if atr[i] > .40 else .02,
                    "stop_pct": .019*(1.3 if macro(bar_time) else 1.)})
    return out


def protected_class(Base):
    class Protected(Base):
        def __init__(self, stress):
            super().__init__("BTCUSDT", stress)
            self.pending_source = None
            self.last_exit_side = 0
            self.last_exit_bar = -1000

        def enter(self, t, target, raw, mark, signal):
            super().enter(t, target, raw, mark, signal)
            if self.position:
                p = self.position
                p["stop_pct"] = self.pending_source["stop_pct"]
                p["tp_pct"] = self.pending_source["tp_pct"]
                p["stop"] = p["entry"] * (1-target*p["stop_pct"])
                p["target"] = p["entry"] * (1+target*p["tp_pct"])
                p["estimated_risk"] = p["qty"]*p["entry"]*(p["stop_pct"]+2*(self.fee+self.impact)+.0021)
                assert p["estimated_risk"] <= 5

        def signal(self, T, target, pair):
            # The registered Pine rule has a direction-specific cooldown,
            # already checked by the caller, not Account's all-side stop gate.
            last_stop = self.last_stop
            self.last_stop = -1
            try:
                super().signal(T, target, pair)
            finally:
                self.last_stop = last_stop

        def close(self, t, raw, reason, liqrate=0.):
            p = self.position
            self.last_exit_side = p["sign"]
            self.last_exit_bar = (t - START - 1) // (15*M)
            super().close(t, raw, reason, liqrate)

        def tick(self, t, bar, mark, events):
            # Existing method executes due orders, funding, then adverse stop.
            super().tick(t, bar, mark, events)
            p = self.position
            if p:
                hit = bar[1] >= p["target"] if p["sign"] == 1 else bar[2] <= p["target"]
                if hit:
                    self.close(t+M, p["target"], "target")
    return Protected


def self_checks(Base, Protected):
    x = np.arange(40, dtype=float)
    np.testing.assert_allclose(rma(np.ones(40), 14)[13:], 1)
    # Feature construction must be invariant to removing future input.
    minute = np.column_stack([100+x/100, 100.1+x/100, 99.9+x/100, 100+x/100])
    minute = np.tile(minute, (150, 1))  # 6000 rows = 400 completed 15m bars
    full = signals(minute)
    assert full[:200] == signals(minute[:3000])
    # An entry minute touching both bracket levels takes the stop, not TP.
    a = Protected(False)
    a.pending_source = {"stop_pct": .019, "tp_pct": .02}
    a.signal(START, 1, None)
    a.tick(START+M, np.array([74000, 77000, 71000, 74000.]),
           np.array([74000, 77000, 71000, 74000.]), [])
    assert len(a.trades) == 1 and a.trades[0]["reason"] == "stop"
    assert math.isclose(a.cash, 1000+a.trades[0]["net"], abs_tol=1e-9)
    # A clean favorable minute hits the full target; no position survives it.
    b = Protected(False)
    b.pending_source = {"stop_pct": .019, "tp_pct": .02}
    b.signal(START, -1, None)
    b.tick(START+M, np.array([74000, 74100, 71000, 72000.]),
           np.array([74000, 74100, 71000, 72000.]), [])
    assert len(b.trades) == 1 and b.trades[0]["reason"] == "target" and b.position is None
    assert b.trades[0]["entry_fee"] > 0 and b.trades[0]["exit_fee"] > 0


def main(root, out):
    configure_period()
    Base, account_sha = load_account_class()
    Protected = protected_class(Base)
    self_checks(Base, Protected)
    trade, mark, fund, proof, conflicts = core.load_data(root, "BTCUSDT")
    if conflicts or not np.isfinite(trade).all() or not np.isfinite(mark).all():
        raise ValueError("missing/conflicting input; do not invent a repair")
    candidates = signals(trade)
    fby = {}
    for t, rate in fund:
        fby.setdefault((t-START)//M, []).append((t, rate))
    summaries, journals, records = {}, {}, {}
    for stress in (False, True):
        a = Protected(stress)
        name = "stress" if stress else "base"
        for i in range((BEGIN-START)//M, len(trade)):
            t = START + i*M
            if i % 15 == 0:
                sig = candidates[i//15-1]
                side = sig["side"]
                if side and not (a.last_exit_side == side and sig["bar_index"]-a.last_exit_bar < 6):
                    a.pending_source = sig
                    a.signal(t, side, None)
            if a.position or a.pending:
                a.tick(t, trade[i], mark[i], fby.get(i, []))
            if a.halted:
                break
        summary = a.finish(float(mark[-1, 3]))
        assert math.isclose(a.cash, 1000+sum(r["net"] for r in a.trades)
                            - (a.position["entry_fee"] if a.position else 0)
                            + (a.position["funding"] if a.position else 0), abs_tol=1e-7)
        summaries[name] = summary
        journals[name] = a.trades
        records[name] = {"events": a.events, "admitted_signals": a.signals}
        print(name, "closed", summary["closed"], "net", summary["net_including_open"], flush=True)
    out.mkdir(parents=True, exist_ok=True)
    all_signals = [s for s in candidates if BEGIN <= s["time_ms"] < END and s["side"]]
    summary = {"protocol": "RSI15M_VERIFICATION_PROTOCOL.md", "account_source_sha256": account_sha,
               "inputs": proof, "conflicts": conflicts,
               "checks": ["RMA seed", "future truncation invariance", "same-minute stop before target", "short target and both fees", "cash reconciliation"],
               "raw_signals": len(all_signals), "raw_long": sum(s["side"] == 1 for s in all_signals),
               "raw_short": sum(s["side"] == -1 for s in all_signals), "results": summaries}
    (out/"summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False)+"\n")
    for name, value in (("trades", journals), ("actions", records), ("raw_signals", all_signals)):
        with gzip.open(out/(name+".json.gz"), "wt") as f:
            json.dump(value, f, allow_nan=False, sort_keys=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
