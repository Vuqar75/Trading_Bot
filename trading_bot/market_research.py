"""Isolated, read-only market research. No worker, wallet or order imports.

Inputs are recorded observations, not evidence that a source is truthful. The
deterministic report is deliberately separate from any model-generated opinion.
"""
import hashlib
import json
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit


INTERVALS_MS = {"5m": 300000, "15m": 900000, "1h": 3600000}
SYMBOLS = {"BTCUSDT", "ETHUSDT"}


class ResearchError(ValueError):
    """Public, fixed error codes only; never include provider response bodies."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def timestamp(value):
    if type(value) is not int or not 0 <= value <= 253402300799999:
        raise ResearchError("invalid_timestamp")
    return value


def number(value, *, zero=False):
    if isinstance(value, bool):
        raise ResearchError("invalid_market_number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ResearchError("invalid_market_number") from None
    if not result.is_finite() or result < 0 or (not zero and result == 0):
        raise ResearchError("invalid_market_number")
    # Avoid pathological inputs and overflow in research-only float statistics.
    if result > Decimal("1e20") or (result and result < Decimal("1e-20")):
        raise ResearchError("market_number_too_large")
    return result


def _evidence(rows, as_of, kind):
    if not isinstance(rows, list) or len(rows) > 50:
        raise ResearchError("invalid_evidence_list")
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ResearchError("invalid_evidence")
        ident = row.get("id")
        if not isinstance(ident, str) or not ident or len(ident) > 80 or ident in seen:
            raise ResearchError("duplicate_or_invalid_source_id")
        seen.add(ident)
        for field, limit in (("title", 300), ("text", 4000), ("publisher", 150), ("url", 1500)):
            if not isinstance(row.get(field), str) or not row[field].strip() or len(row[field]) > limit:
                raise ResearchError("invalid_source_text")
        url = urlsplit(row["url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ResearchError("invalid_source_url")
        # These are declared publication/observation times; no retrospective
        # backdating of today's downloaded news into yesterday's snapshot.
        published = timestamp(row.get("published_at_ms"))
        observed = timestamp(row.get("first_seen_at_ms"))
        if published > observed or observed > as_of:
            raise ResearchError("evidence_not_available_at_cutoff")
        if row.get("source_type") not in {"primary", "secondary", "synthetic"}:
            raise ResearchError("invalid_source_type")
        assets = row.get("assets")
        if not isinstance(assets, list) or not assets or any(a not in {"BTC", "ETH", "MARKET"} for a in assets):
            raise ResearchError("invalid_source_assets")
        result.append({key: row[key] for key in (
            "id", "title", "text", "publisher", "url", "published_at_ms",
            "first_seen_at_ms", "source_type", "assets")} | {"kind": kind})
    return result


def _technical(rows, interval_ms, as_of):
    if not isinstance(rows, list) or not 31 <= len(rows) <= 200:
        raise ResearchError("need_31_to_200_candles")
    closed = []
    previous = None
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            raise ResearchError("invalid_candle")
        start, end = timestamp(row[0]), timestamp(row[6])
        if start % interval_ms or end != start + interval_ms - 1:
            raise ResearchError("invalid_candle_boundary")
        if previous is not None and start != previous + interval_ms:
            raise ResearchError("duplicate_or_missing_candle")
        previous = start
        o, h, low, c = [number(v) for v in row[1:5]]
        volume = number(row[5], zero=True)
        if low > min(o, c) or h < max(o, c) or low > h:
            raise ResearchError("invalid_ohlc")
        if end >= as_of:
            # A partially formed candle cannot contribute to statistics.
            continue
        closed.append([start, str(o), str(h), str(low), str(c), str(volume), end])
    if len(closed) < 31:
        raise ResearchError("not_enough_closed_candles")
    if as_of - closed[-1][6] > interval_ms + 5000:
        raise ResearchError("stale_candles")
    prices = [float(r[4]) for r in closed]
    highs = [float(r[2]) for r in closed]
    lows = [float(r[3]) for r in closed]
    volumes = [float(r[5]) for r in closed]
    changes = [b - a for a, b in zip(prices[-15:-1], prices[-14:])]
    gain = sum(max(d, 0) for d in changes) / 14
    loss = sum(max(-d, 0) for d in changes) / 14
    rsi = 50.0 if gain == loss == 0 else (100.0 if loss == 0 else 100 - 100 / (1 + gain / loss))
    ranges = [max(highs[i] - lows[i], abs(highs[i] - prices[i-1]), abs(lows[i] - prices[i-1]))
              for i in range(len(closed)-14, len(closed))]
    returns = [math.log(b / a) for a, b in zip(prices[-21:-1], prices[-20:])]
    mean = sum(returns) / len(returns)
    baseline_volume = sum(volumes[-21:-1]) / 20
    stats = {
        "last_close": closed[-1][4], "last_closed_at_ms": closed[-1][6],
        "closed_count": len(closed), "sma10": sum(prices[-10:]) / 10,
        "sma30": sum(prices[-30:]) / 30, "rsi14_simple": rsi,
        "atr14_simple": sum(ranges) / 14,
        "return_20_bars_pct": (prices[-1] / prices[-21] - 1) * 100,
        "volatility_20_bars": math.sqrt(sum((v-mean)**2 for v in returns) / len(returns)),
        "volume_ratio_to_prior20": volumes[-1] / baseline_volume if baseline_volume else None,
        "support_prior20": min(lows[-21:-1]), "resistance_prior20": max(highs[-21:-1]),
    }
    return closed, stats


def build_snapshot(raw):
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ResearchError("invalid_snapshot_version")
    as_of = timestamp(raw.get("as_of_ms"))
    if timestamp(raw.get("captured_at_ms")) > as_of:
        raise ResearchError("capture_after_cutoff")
    interval = raw.get("interval")
    if interval not in INTERVALS_MS:
        raise ResearchError("unsupported_interval")
    markets = raw.get("markets")
    if not isinstance(markets, dict) or not markets or not set(markets) <= SYMBOLS:
        raise ResearchError("unsupported_market")
    output = {}
    for symbol, row in markets.items():
        if not isinstance(row, dict) or timestamp(row.get("observed_at_ms")) > as_of:
            raise ResearchError("market_not_available_at_cutoff")
        if row.get("source") not in {"binance_public_spot", "synthetic"}:
            raise ResearchError("unsupported_market_source")
        candles, stats = _technical(row.get("candles"), INTERVALS_MS[interval], as_of)
        output[symbol] = {"source": row["source"], "observed_at_ms": row["observed_at_ms"],
                          "closed_candles": candles, "technical": stats}
    news = _evidence(raw.get("news", []), as_of, "news")
    fundamentals = _evidence(raw.get("fundamentals", []), as_of, "fundamental")
    ids = [r["id"] for r in news + fundamentals]
    if len(set(ids)) != len(ids):
        raise ResearchError("duplicate_source_id_across_categories")
    snapshot = {"version": 1, "as_of_ms": as_of, "interval": interval,
                "captured_at_ms": raw["captured_at_ms"], "markets": output,
                "news": news, "fundamentals": fundamentals,
                "collection_status": raw.get("collection_status", {}),
                "limitations": [
                    "Spot candles do not describe derivatives positioning, liquidity or funding.",
                    "Source labels and timestamps are supplied observations, not verified truth.",
                    "Missing news/fundamentals mean missing coverage, not absence of events.",
                    "Historical model commentary is not a leakage-free strategy backtest.",
                    "This observer cannot trade, resume markets or change risk settings.",
                ]}
    snapshot["snapshot_id"] = hashlib.sha256(canonical(snapshot).encode()).hexdigest()
    return snapshot


def offline_report(snapshot):
    """No model call and no simulated AI opinion."""
    return {"mode": "offline_deterministic", "snapshot_id": snapshot["snapshot_id"],
            "as_of_ms": snapshot["as_of_ms"], "model_called": False,
            "technical": {s: m["technical"] for s, m in snapshot["markets"].items()},
            "news_coverage": {"provided": len(snapshot["news"]), "complete": False},
            "fundamental_coverage": {"provided": len(snapshot["fundamentals"]), "complete": False},
            "collection_status": snapshot["collection_status"],
            "sources": snapshot["news"] + snapshot["fundamentals"],
            "limitations": snapshot["limitations"]}


def render_report(report):
    stamp = datetime.fromtimestamp(report["as_of_ms"] / 1000, timezone.utc).isoformat()
    lines = ["# Исследование рынка — offline", "", f"Срез: {stamp}",
             f"Идентификатор: `{report['snapshot_id']}`", "",
             "Модель ИИ не вызывалась. Торговых решений этот отчёт не исполняет.", ""]
    for symbol, t in report["technical"].items():
        lines += [f"## {symbol}", "", f"Закрытие: {t['last_close']} USDT; свечей: {t['closed_count']}.",
                  f"SMA10: {t['sma10']:.4f}; SMA30: {t['sma30']:.4f}; RSI14 (simple): {t['rsi14_simple']:.2f}.",
                  f"ATR14 (simple): {t['atr14_simple']:.4f}; изменение за 20 свечей: {t['return_20_bars_pct']:.4f}%.", ""]
    lines += [f"Новостных источников: {report['news_coverage']['provided']}; фундаментальных: {report['fundamental_coverage']['provided']}.",
              "Полнота покрытия не подтверждена. Пустой список не означает отсутствие событий.", ""]
    if report["collection_status"]:
        lines += ["Состояние источников: " + canonical(report["collection_status"]), ""]
    for r in report["sources"]:
        # JSON quoting keeps untrusted text out of Markdown link syntax.
        lines += [canonical({k: r[k] for k in ("id", "publisher", "title", "url", "source_type")}), ""]
    lines += ["Ограничения:", ""] + ["- " + s for s in report["limitations"]]
    return "\n".join(lines) + "\n"
