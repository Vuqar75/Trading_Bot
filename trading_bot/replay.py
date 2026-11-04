"""Offline minute-open replay. Never loads env, credentials, a live DB or Binance.

At time t only candle opens at t and candles CLOSED before t are visible. High,
low and close of the current candle are unavailable to the execution engine.
"""
import argparse
import csv
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from .core import Config, Engine, Store


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int


def load_candles(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        candles = [Candle(int(r["open_time"]), *(float(r[k]) for k in
                    ("open", "high", "low", "close", "volume")), int(r["close_time"]))
                   for r in csv.DictReader(f)]
    if len(candles) < 33:
        raise ValueError("Need at least 33 consecutive minute candles")
    for i, c in enumerate(candles):
        if c.open_time % 60000 or c.close_time != c.open_time + 59999:
            raise ValueError("Invalid minute boundaries")
        if not all(math.isfinite(x) and x > 0 for x in (c.open,c.high,c.low,c.close)):
            raise ValueError("Invalid OHLC price")
        if not math.isfinite(c.volume) or c.volume < 0 or not c.low <= min(c.open,c.close) <= max(c.open,c.close) <= c.high:
            raise ValueError("Invalid OHLCV bounds")
        if i and c.open_time != candles[i-1].open_time + 60000:
            raise ValueError("Gap, duplicate or unsorted minute candles; no interpolation allowed")
    return candles


@dataclass(frozen=True)
class Variant:
    name: str = "baseline"
    slope: bool = False
    min_slope_bps: float = 0.0
    confirm: bool = False
    opposite: bool = False


class ReplayExchange:
    """Expose closed prices only; cannot place orders or fetch data."""
    def __init__(self):
        self.current_open = None
        self.closed = []

    def advance(self, current, previous):
        if previous and previous[-1].close_time >= current.open_time:
            raise ValueError("Future candle supplied")
        self.current_open = current.open
        self.closed = [(c.open_time,c.close) for c in previous[-32:]]

    def price(self, symbol):
        return self.current_open

    def candles(self, symbol):
        return list(self.closed)

    def call(self, *args, **kwargs):
        raise AssertionError("Offline replay cannot submit orders")


class ReplayEngine(Engine):
    def __init__(self, cfg, store, exchange, variant):
        if cfg.mode != "paper":
            raise ValueError("Replay is paper-only")
        super().__init__(cfg,store,exchange)
        self.variant = variant
        self.pending_entry = None
        self.next_direction = None

    def strategy_metadata(self):
        return {**super().strategy_metadata(), "offline_variant": asdict(self.variant)}

    def resolve_action(self, details, stamp, position):
        raw = details["action"]
        if position:
            self.pending_entry = None
            return raw  # Never delay protective/reversal exits for an entry filter.
        if "fast" not in details:
            return "HOLD"
        regime = "BUY" if details["fast"] > details["slow"] else "SELL" if details["fast"] < details["slow"] else "HOLD"
        candidate = None
        # Reentry uses the NEXT closed candle, never the closing candle itself.
        if self.next_direction:
            direction, previous_stamp = self.next_direction
            if stamp > previous_stamp:
                self.next_direction = None
                if regime == direction:
                    candidate = direction
        if self.variant.confirm and self.pending_entry:
            direction, previous_stamp = self.pending_entry
            if stamp > previous_stamp:
                self.pending_entry = None
                if stamp == previous_stamp + 60000 and regime == direction:
                    candidate = direction
        if candidate is None and raw != "HOLD":
            if self.variant.confirm:
                self.pending_entry = (raw,stamp)
                return "HOLD"
            candidate = raw
        if candidate is None:
            return "HOLD"
        if self.variant.slope:
            slope = (details["fast"] / details["old_fast"] - 1) * 10000
            signed = slope if candidate == "BUY" else -slope
            if signed <= self.variant.min_slope_bps:
                return "HOLD"
        return candidate

    def futures_signal(self, action, price, stamp, position, prices):
        result = super().futures_signal(action,price,stamp,position,prices)
        if self.variant.opposite and position and self.store.get("position") is None:
            self.next_direction = (action,stamp)
        return result


def run_replay(candles, start_ms, end_ms, variant=Variant(), *, entry_fee_bps=10,
               exit_fee_bps=10, slippage_bps=0, amount=10, leverage=1,
               engine_class=ReplayEngine, capture=False):
    """Flat start; preceding candles warm indicators only. End liquidation is explicit.

    End is an available minute OPEN. Close any remaining position there, with costs,
    without allowing a new strategy entry. This boundary quote is not fitted on.
    """
    if not 1 <= leverage <= 10 or type(leverage) is not int:
        raise ValueError("Leverage must be integer 1..10")
    times = {c.open_time:i for i,c in enumerate(candles)}
    if start_ms not in times or end_ms not in times or start_ms >= end_ms:
        raise ValueError("Period boundaries must be available candle opens")
    if times[start_ms] < 31:
        raise ValueError("At least 31 warmup candles required")
    clock = [start_ms/1000]
    store = Store(":memory:", clock=lambda:clock[0])
    exchange = ReplayExchange()
    cfg = Config(amount=amount, db=":memory:", entry_fee_bps=entry_fee_bps,
                 exit_fee_bps=exit_fee_bps, slippage_bps=slippage_bps)
    engine = engine_class(cfg,store,exchange,variant)
    engine.change_market("futures")
    engine.change_leverage(leverage)
    store.set("paused",False)
    peak, drawdown, max_dd_pct = 1000.0, 0.0, 0.0
    equity_curve = []
    def mark(quote, stamp):
        nonlocal peak, drawdown, max_dd_pct
        equity = store.get("futures_balance",1000.0)
        pos = store.get("position")
        if pos:
            sign = 1 if pos["direction"] == "LONG" else -1
            costs = cfg.paper_costs()
            fill = costs.fill(quote,pos["direction"],False)
            equity += max(0,pos["margin"] + sign*(fill-pos["price"])*pos["quantity"]
                          - pos["entry_fee"] - fill*pos["quantity"]*costs.exit_fee_bps/10000)
        peak = max(peak,equity)
        drawdown = max(drawdown,peak-equity)
        max_dd_pct = max(max_dd_pct,(peak-equity)/peak*100)
        if capture: equity_curve.append({"ts":stamp,"equity":equity})
    try:
        # No AI/network calls, even if the invoking shell happens to enable AI.
        with patch("trading_bot.core.ai_allows",return_value=True):
            for i in range(times[start_ms],times[end_ms]):
                current = candles[i]
                clock[0] = current.open_time/1000
                exchange.advance(current,candles[max(0,i-32):i])
                engine.tick()
                mark(current.open,clock[0])
        boundary = candles[times[end_ms]]
        clock[0] = end_ms/1000
        forced_close = bool(store.get("position"))
        if forced_close:
            engine.close_position(boundary.open,store.get("candle"),"Replay period end")
        mark(boundary.open,clock[0])
        store.db.row_factory = __import__('sqlite3').Row
        journal = [dict(r) for r in store.db.execute("SELECT * FROM execution_journal ORDER BY id")]
        closes = [r for r in journal if r["event"] == "CLOSE"]
        fees = sum(r["entry_fee"]+r["exit_fee"] for r in closes)
        gross = sum(r["gross_pnl"] for r in closes)
        net = sum(r["net_pnl"] for r in closes)
        slips = sum(r["slippage_cost"] for r in journal)
        adjustment = sum(r["loss_cap_adjustment"] for r in closes)
        result = dict(variant=asdict(variant),start_ms=start_ms,end_ms=end_ms,
                      costs=cfg.paper_costs().as_dict(),trades=len(closes),
                      long_trades=sum(r["direction"]=="LONG" for r in closes),
                      short_trades=sum(r["direction"]=="SHORT" for r in closes),
                      gross_pnl=gross,fees=fees,net_pnl=net,slippage_cost=slips,
                      gross_before_slippage=gross+slips,loss_cap_adjustment=adjustment,
                      max_drawdown=drawdown,max_drawdown_pct=max_dd_pct,
                      fees_share_of_loss_pct=fees/(-net)*100 if net < -1e-12 else None,
                      fees_share_of_costs_pct=fees/(fees+slips)*100 if fees+slips else None,
                      forced_end_close=forced_close,
                      reconciliation_error=net-(gross-fees+adjustment),
                      balance_error=store.get("futures_balance",1000.0)-(1000+net))
        if capture: result.update(journal=journal,equity=equity_curve)
        return result
    finally:
        store.db.close()


def variants():
    return [Variant(),Variant("confirm",confirm=True),Variant("opposite",opposite=True)] + [
        Variant(name,slope=True,min_slope_bps=threshold,confirm=name=="combined",opposite=name=="combined")
        for name in ("slope","combined") for threshold in (0.0,0.5,1.0)]


def compare(candles, split_ms, *, capture=False, entry_fee_bps=10, exit_fee_bps=10,
            slippage_bps=0, stress_slippage_bps=2):
    # Last candle's OPEN is the end quote, so its high/low/close are never consulted.
    start, end = candles[31].open_time,candles[-1].open_time
    if not start < split_ms < end:
        raise ValueError("Train/holdout split outside available data")
    # Training liquidation also precedes the split; do not consume the first
    # holdout open as a training fill or as part of parameter selection.
    train_end = split_ms - 60000
    costs = dict(entry_fee_bps=entry_fee_bps,exit_fee_bps=exit_fee_bps,slippage_bps=slippage_bps)
    train = [run_replay(candles,start,train_end,v,**costs) for v in variants()]
    selected = {}
    for row in train:
        name = row["variant"]["name"]
        rank = (row["net_pnl"],-row["max_drawdown"],-row["variant"]["min_slope_bps"])
        old = selected.get(name)
        if old is None or rank > (old["net_pnl"],-old["max_drawdown"],-old["variant"]["min_slope_bps"]):
            selected[name] = row
    winner = max(selected.values(),key=lambda r:(r["net_pnl"],-r["max_drawdown"]))["variant"]
    # Freeze all family choices and the overall winner BEFORE any holdout run.
    frozen = [Variant(**row["variant"]) for row in selected.values()]
    holdout = [run_replay(candles,split_ms,end,v,capture=capture,**costs) for v in frozen]
    stress = [run_replay(candles,split_ms,end,v,**{**costs,"slippage_bps":stress_slippage_bps}) for v in frozen]
    return dict(train_candidates=train,selected_on_train=[asdict(v) for v in frozen],
                winner_on_train=winner,holdout=holdout,holdout_stress=stress,
                selection_rule="Max train net PnL; ties: lower drawdown then lower slope threshold. No holdout tuning.",
                limitations=["Minute-open proxy, not exact live polling/ticker fills.",
                             "Stops checked at minute opens, not intrabar highs/lows.",
                             "Equity drawdown sampled once per minute, including estimated exit costs.",
                             "Flat restart for each period, warmup from past only; forced close at period boundary.",
                             "Futures simulation on Spot Testnet; no funding, spread/orderbook or market impact.",
                             "Fees/loss ratio can exceed 100% when gross PnL offsets fees; null for nonnegative net."])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candles",required=True,type=Path)
    parser.add_argument("--split",required=True,help="UTC ISO date, e.g. 2026-10-20")
    parser.add_argument("--output",required=True,type=Path)
    parser.add_argument("--entry-fee-bps",type=float,default=10)
    parser.add_argument("--exit-fee-bps",type=float,default=10)
    parser.add_argument("--slippage-bps",type=float,default=0)
    parser.add_argument("--stress-slippage-bps",type=float,default=2)
    args = parser.parse_args()
    candles = load_candles(args.candles)
    split = int(datetime.fromisoformat(args.split).replace(tzinfo=timezone.utc).timestamp()*1000)
    report = compare(candles,split,capture=True,entry_fee_bps=args.entry_fee_bps,
                     exit_fee_bps=args.exit_fee_bps,slippage_bps=args.slippage_bps,
                     stress_slippage_bps=args.stress_slippage_bps)
    report["input"] = dict(file=args.candles.name,sha256=hashlib.sha256(args.candles.read_bytes()).hexdigest(),
                           rows=len(candles),first_open_ms=candles[0].open_time,last_open_ms=candles[-1].open_time)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"winner_on_train":report["winner_on_train"],"holdout":[{k:r[k] for k in ("variant","trades","net_pnl","max_drawdown","fees","fees_share_of_loss_pct")} for r in report["holdout"]]},indent=2))


if __name__ == "__main__":
    main()
