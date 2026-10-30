import hashlib
import hmac
import json
import os
import math
import sqlite3
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from .execution import PaperCosts


def request(url, data=None, headers=None):
    body = json.dumps(data).encode() if data is not None else None
    req = Request(url, data=body, headers={"Content-Type": "application/json", **(headers or {})})
    with urlopen(req, timeout=20) as response:
        return json.load(response)


@dataclass
class Config:
    mode: str = "paper"
    symbol: str = "BTCUSDT"
    amount: float = 10
    daily_loss: float = 5
    stop: float = 2
    take: float = 4
    db: str = "data/bot.db"
    entry_fee_bps: float = 10.0
    exit_fee_bps: float = 10.0
    slippage_bps: float = 0.0

    def paper_costs(self):
        return PaperCosts(self.entry_fee_bps, self.exit_fee_bps, self.slippage_bps)

    @classmethod
    def load(cls):
        cfg = cls(os.getenv("BOT_MODE", "paper"), os.getenv("SYMBOL", "BTCUSDT"),
                  float(os.getenv("TRADE_USDT", "10")), float(os.getenv("MAX_DAILY_LOSS_USDT", "5")),
                  float(os.getenv("STOP_LOSS_PCT", "2")), float(os.getenv("TAKE_PROFIT_PCT", "4")),
                  os.getenv("DB_PATH", "data/bot.db"),
                  float(os.getenv("PAPER_ENTRY_FEE_BPS", "10")),
                  float(os.getenv("PAPER_EXIT_FEE_BPS", "10")),
                  float(os.getenv("PAPER_SLIPPAGE_BPS", "0")))
        cfg.paper_costs()
        if cfg.mode not in {"paper", "testnet"}:
            raise ValueError("Поддерживаются только paper и testnet; реальная торговля отключена")
        if not all(math.isfinite(v) for v in (cfg.amount, cfg.daily_loss, cfg.stop, cfg.take)) or cfg.amount <= 0 or cfg.daily_loss <= 0 or not 0 < cfg.stop < 100 or cfg.take <= 0:
            raise ValueError("Некорректные лимиты риска")
        return cfg


class Store:
    def __init__(self, path, clock=None):
        self.clock = clock or (lambda: time.time())
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS trades (ts REAL, symbol TEXT, side TEXT, quantity REAL, price REAL, pnl REAL, mode TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY, text TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS futures_trades (ts REAL, symbol TEXT, direction TEXT, event TEXT, quantity REAL, price REAL, pnl REAL, leverage INTEGER, margin REAL, reason TEXT)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS execution_journal (
            id INTEGER PRIMARY KEY, position_id TEXT NOT NULL, ts REAL NOT NULL,
            symbol TEXT NOT NULL, market TEXT NOT NULL, direction TEXT NOT NULL,
            event TEXT NOT NULL, quantity REAL NOT NULL, quote_price REAL NOT NULL,
            fill_price REAL NOT NULL, entry_fee REAL NOT NULL, exit_fee REAL NOT NULL,
            gross_pnl REAL NOT NULL, net_pnl REAL NOT NULL, slippage_cost REAL NOT NULL,
            loss_cap_adjustment REAL NOT NULL, leverage INTEGER NOT NULL,
            margin REAL NOT NULL, reason TEXT NOT NULL, candle INTEGER,
            signal_json TEXT NOT NULL, costs_json TEXT NOT NULL, holding_seconds REAL,
            legacy_position INTEGER NOT NULL DEFAULT 0,
            strategy_json TEXT NOT NULL DEFAULT '{}')""")
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def loss(self):
        start = int(self.clock() // 86400) * 86400
        spot = self.db.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE ts>=?", (start,)).fetchone()[0]
        futures = self.db.execute("SELECT COALESCE(SUM(pnl),0) FROM futures_trades WHERE ts>=?", (start,)).fetchone()[0]
        return -(spot + futures)

    def history(self, limit=10):
        return self.db.execute(
            "SELECT ts,symbol,side,quantity,price,pnl,mode FROM ("
            "SELECT ts,symbol,side,quantity,price,pnl,mode FROM trades UNION ALL "
            "SELECT ts,symbol,event || ' ' || direction,quantity,price,pnl,"
            "'paper futures ' || leverage || 'x' FROM futures_trades) ORDER BY ts DESC LIMIT ?",
            (limit,)).fetchall()

    def report_error(self, stage, exc):
        # Do not persist exception bodies or URLs: they can contain API tokens.
        code = getattr(exc, "code", None)
        reason = type(exc).__name__ + (f" HTTP {code}" if isinstance(code, int) else "")
        now = self.clock()
        key = "error_notice_" + stage
        previous = self.get(key, {})
        message = f"Ошибка ({stage}): {reason}. Проверьте /status."
        if stage == "торговля":
            message += " Новые входы приостановлены; контроль позиции будет повторяться."
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)",
                            ("last_error", json.dumps({"time": now, "stage": stage, "reason": reason})))
            # Repeated failures do not create a message every polling cycle.
            if previous.get("reason") != reason or now - previous.get("time", 0) >= 900:
                self.db.execute("INSERT INTO notifications(text) VALUES (?)", (message,))
                self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)",
                                (key, json.dumps({"time": now, "reason": reason})))


class Binance:
    # Fixed host: real trading cannot be selected by configuration.
    base = "https://testnet.binance.vision"

    def call(self, path, params=None, signed=False, post=False):
        params = dict(params or {})
        headers = {}
        if signed:
            key, secret = os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET")
            if not key or not secret:
                raise ValueError("Не настроены ключи Binance Spot Testnet")
            params.update(timestamp=int(time.time() * 1000), recvWindow=5000)
            headers["X-MBX-APIKEY"] = key
        query = urlencode(params)
        if signed:
            query += "&signature=" + hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        req = Request(self.base + path + ("?" + query if query else ""), headers=headers,
                      method="POST" if post else "GET")
        with urlopen(req, timeout=20) as response:
            return json.load(response)

    def symbol(self, symbol):
        rows = self.call("/api/v3/exchangeInfo", {"symbol": symbol})["symbols"]
        if not rows or rows[0]["status"] != "TRADING" or rows[0]["quoteAsset"] != "USDT":
            raise ValueError("Нужна доступная спотовая пара с котировкой USDT")
        return rows[0]

    def candles(self, symbol):
        rows = self.call("/api/v3/klines", {"symbol": symbol, "interval": "1m", "limit": 32})
        return [(int(r[0]), float(r[4])) for r in rows if int(r[6]) < time.time() * 1000]

    def price(self, symbol):
        return float(self.call("/api/v3/ticker/price", {"symbol": symbol})["price"])


def signal_details(prices):
    if len(prices) < 31:
        return {"action": "HOLD", "count": len(prices)}
    old_fast, old_slow = sum(prices[-11:-1]) / 10, sum(prices[-31:-1]) / 30
    fast, slow = sum(prices[-10:]) / 10, sum(prices[-30:]) / 30
    action = "HOLD"
    if old_fast <= old_slow and fast > slow:
        action = "BUY"
    elif old_fast >= old_slow and fast < slow:
        action = "SELL"
    return {"action": action, "count": len(prices), "old_fast": old_fast,
            "old_slow": old_slow, "fast": fast, "slow": slow}


def signal(prices):
    return signal_details(prices)["action"]


def ai_allows(symbol, prices, direction="BUY", market="spot"):
    if os.getenv("AI_ENABLED", "false").lower() != "true":
        return True
    key, model = os.getenv("GEMINI_API_KEY"), os.getenv("GEMINI_MODEL")
    if not key or not model:
        return False
    try:
        result = request("https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent",
                         {"contents": [{"parts": [{"text": f"Evaluate this experimental {market} {direction}. Return JSON {{\"allow\":true|false}}. Symbol: " + symbol + "; closed minute prices: " + json.dumps(prices)}]}],
                          "generationConfig": {"responseMimeType": "application/json", "temperature": 0}},
                         {"x-goog-api-key": key})
        decision = json.loads(result["candidates"][0]["content"]["parts"][0]["text"])
        return decision.get("allow") is True
    except Exception:
        return False


class Engine:
    def __init__(self, cfg, store, exchange):
        self.cfg, self.store, self.exchange = cfg, store, exchange
        self.clock = store.clock
        cfg.paper_costs()
        identity = {"mode": cfg.mode, "amount": cfg.amount}
        previous = store.get("identity")
        if previous and previous != identity:
            raise ValueError("Для другого режима/размера сделки используйте отдельную базу DB_PATH")
        store.set("identity", identity)
        self.symbol = store.get("symbol", cfg.symbol)
        self.market = store.get("market", "spot")
        self.leverage = store.get("leverage", 1)
        if self.market not in {"spot", "futures"} or type(self.leverage) is not int or not 1 <= self.leverage <= 10:
            raise ValueError("Некорректные настройки рынка или плеча")
        if self.market == "futures" and cfg.mode != "paper":
            raise ValueError("Фьючерсы доступны только в симуляции paper")

    def require_flat(self):
        if self.store.get("position") or self.store.get("pending"):
            raise ValueError("Сначала закройте позицию и разрешите незавершённый ордер")

    def strategy_metadata(self):
        return {"name": "sma-cross-baseline", "fast": 10, "slow": 30,
                "interval": "1m", "stop_pct": self.cfg.stop,
                "take_pct": self.cfg.take, "daily_loss_usdt": self.cfg.daily_loss,
                "amount_usdt": self.cfg.amount}

    def change_market(self, market):
        self.require_flat()
        if market not in {"spot", "futures"}:
            raise ValueError("Используйте /mode spot или /mode futures")
        if market == "futures" and self.cfg.mode != "paper":
            raise ValueError("Фьючерсы доступны только в симуляции paper")
        self.store.set("market", market)
        self.market = market
        self.store.set("paused", True)
        self.store.set("candle", None)
        self.store.set("signal_snapshot", None)

    def change_leverage(self, leverage):
        self.require_flat()
        if self.market != "futures" or self.cfg.mode != "paper":
            raise ValueError("Плечо доступно только в симуляции фьючерсов")
        if type(leverage) is not int or not 1 <= leverage <= 10:
            raise ValueError("Плечо должно быть целым числом от 1 до 10")
        self.leverage = leverage
        self.store.set("leverage", leverage)
        self.store.set("paused", True)

    def close_position(self, price, candle=None, reason="Ручное закрытие"):
        if self.store.get("pending"):
            raise ValueError("Требуется сверка незавершённого ордера")
        if self.market == "futures":
            self._snapshot(reason, action="CLOSE", candle=candle)
            return self.futures_trade("CLOSE", price, candle, reason)
        return self.trade("SELL", price, candle)

    def futures_trade(self, action, price, candle, reason="Сигнал стратегии"):
        if self.cfg.mode != "paper" or self.market != "futures":
            raise ValueError("Доступна только симуляция фьючерсов")
        if self.store.get("pending"):
            raise ValueError("Требуется сверка ордера")
        if action not in {"LONG", "SHORT", "CLOSE"} or not math.isfinite(price) or price <= 0:
            raise ValueError("Некорректное действие или цена")
        position = self.store.get("position")
        balance = self.store.get("futures_balance", 1000.0)
        costs, quote, now = self.cfg.paper_costs(), price, self.clock()
        gross, exit_fee, adjustment = 0.0, 0.0, 0.0
        holding = None
        if action != "CLOSE":
            if position or self.store.get("paused", True) or self.store.loss() >= self.cfg.daily_loss:
                return False
            margin = self.cfg.amount
            if balance < margin:
                return False
            price = costs.fill(quote, action, opening=True)
            quantity = margin * self.leverage / price
            position = {"market": "futures", "direction": action, "price": price,
                        "quantity": quantity, "cost": margin * self.leverage,
                        "margin": margin, "leverage": self.leverage,
                        "entry_fee": margin * self.leverage * costs.entry_fee_bps / 10000,
                        "position_id": uuid.uuid4().hex, "opened_at": now,
                        "entry_quote": quote, "entry_slippage_cost": abs(price-quote)*quantity,
                        "entry_costs": costs.as_dict()}
            balance -= margin
            pnl, event, new_position = 0, "OPEN", position
        else:
            if not position:
                return False
            quantity, margin = position["quantity"], position["margin"]
            price = costs.fill(quote, position["direction"], opening=False)
            sign = 1 if position["direction"] == "LONG" else -1
            gross = sign * (price - position["price"]) * quantity
            exit_fee = quantity * price * costs.exit_fee_bps / 10000
            raw_return = margin + gross - position["entry_fee"] - exit_fee
            returned = max(0, raw_return)
            adjustment = returned - raw_return
            pnl = returned - margin
            balance += returned
            event, new_position = "CLOSE", None
            if "opened_at" in position:
                holding = now - position["opened_at"]
        notice = (f"Симуляция фьючерсов: {event} {position['direction']} {self.symbol}\n"
                  f"Изолированная маржа: {margin:.2f} USDT | {position['leverage']}×\n"
                  f"Количество: {quantity:.8f} | Цена: {price:.8f} USDT\n{reason}")
        if event == "CLOSE":
            notice += f"\nРезультат с модельной комиссией: {pnl:+.4f} USDT"
        with self.store.db:
            self.store.db.execute("INSERT INTO futures_trades VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  (now, self.symbol, position["direction"], event, quantity, price, pnl, position["leverage"], margin, reason))
            snapshot = self.store.get("signal_snapshot") or {}
            # Persist only strategy fields, never environment/account/Telegram data.
            safe_signal = {k: snapshot[k] for k in ("source", "interval", "time", "candle", "action", "raw_action", "count", "old_fast", "old_slow", "fast", "slow") if k in snapshot}
            self.store.db.execute("""INSERT INTO execution_journal
                (position_id,ts,symbol,market,direction,event,quantity,quote_price,fill_price,
                 entry_fee,exit_fee,gross_pnl,net_pnl,slippage_cost,loss_cap_adjustment,
                 leverage,margin,reason,candle,signal_json,costs_json,holding_seconds,legacy_position,strategy_json)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (position.get("position_id", "legacy-" + str(position["price"]) + "-" + str(quantity)),
                 now,self.symbol,"futures",position["direction"],event,quantity,quote,price,
                 position["entry_fee"],exit_fee,gross,pnl,abs(price-quote)*quantity,adjustment,
                 position["leverage"],margin,reason,candle,json.dumps(safe_signal),
                 json.dumps({"current": costs.as_dict(), "entry": position.get("entry_costs")}),
                 holding,int("position_id" not in position),json.dumps(self.strategy_metadata())))
            for key, value in (("position", new_position), ("futures_balance", balance), ("candle", candle)):
                self.store.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))
            self.store.db.execute("INSERT INTO notifications(text) VALUES (?)", (notice,))
        return True

    def change_symbol(self, symbol):
        if self.store.get("position") or self.store.get("pending"):
            raise ValueError("Сначала закройте позицию и разрешите незавершённый ордер")
        self.exchange.symbol(symbol)
        self.symbol = symbol
        self.store.set("symbol", symbol)
        self.store.set("candle", None)
        self.store.set("quote", None)
        self.store.set("signal_snapshot", None)

    def trade(self, side, price, candle):
        if self.market != "spot":
            raise ValueError("Для фьючерсов используется отдельный симулятор")
        position = self.store.get("position")
        if side == "BUY" and (position or self.store.loss() >= self.cfg.daily_loss):
            return
        if side == "SELL" and not position:
            return
        quantity = self.cfg.amount / price if side == "BUY" else position["quantity"]
        fill = price
        if self.cfg.mode == "testnet":
            # Persist intent BEFORE submitting. Never blindly retry ambiguous orders.
            client_id = "tb" + str(time.time_ns())
            params = {"symbol": self.symbol, "side": side, "type": "MARKET", "newClientOrderId": client_id, "newOrderRespType": "FULL"}
            if side == "BUY":
                params["quoteOrderQty"] = str(self.cfg.amount)
            else:
                info = self.exchange.symbol(self.symbol)
                lot = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
                step = Decimal(lot["stepSize"])
                rounded = (Decimal(str(quantity)) / step).to_integral_value(rounding=ROUND_DOWN) * step
                params["quantity"] = format(rounded, "f")
            self.store.set("pending", {"client_id": client_id, "symbol": self.symbol, "side": side})
            result = self.exchange.call("/api/v3/order", params, signed=True, post=True)
            if result["status"] != "FILLED":
                raise RuntimeError("Ордер не исполнен полностью; требуется сверка")
            quantity = float(result["executedQty"])
            fill = float(result["cummulativeQuoteQty"]) / quantity
            if side == "BUY":
                quantity -= sum(float(f["commission"]) for f in result.get("fills", []) if f["commissionAsset"] == self.symbol[:-4])
        # Paper execution includes a 0.1% commission per side.
        cost = quantity * fill
        pnl = 0 if side == "BUY" else (fill - position["price"]) * quantity - cost * 0.001 - position["cost"] * 0.001
        new_position = {"quantity": quantity, "price": fill, "cost": cost} if side == "BUY" else None
        with self.store.db:
            self.store.db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?)", (self.clock(), self.symbol, side, quantity, fill, pnl, self.cfg.mode))
            for key, value in (("position", new_position), ("pending", None), ("candle", candle)):
                self.store.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))
            label = "Виртуальная сделка" if self.cfg.mode == "paper" else "Сделка Testnet"
            action = "Покупка" if side == "BUY" else "Продажа"
            notice = f"{label}: {action} {self.symbol}\nКоличество: {quantity:.8f}\nЦена: {fill:.8f} USDT"
            if side == "SELL":
                notice += f"\nРасчётный результат с комиссией: {pnl:+.4f} USDT"
            self.store.db.execute("INSERT INTO notifications(text) VALUES (?)", (notice,))

    def tick(self):
        try:
            self._tick()
        except Exception as exc:
            snapshot = self.store.get("signal_snapshot") or self._snapshot("Проверка ещё не завершена")
            snapshot["check_error"] = type(exc).__name__
            snapshot["checked_at"] = self.clock()
            self.store.set("signal_snapshot", snapshot)
            raise

    def _snapshot(self, reason, **details):
        snapshot = {"symbol": self.symbol, "source": "Binance Spot Testnet",
                    "market": self.market, "interval": "1m", "time": self.clock(), "reason": reason, **details}
        self.store.set("signal_snapshot", snapshot)
        return snapshot

    def resolve_action(self, details, stamp, position):
        """Default strategy unchanged; offline replay may override entry decisions."""
        return details["action"]

    def _tick(self):
        if self.store.get("pending"):
            self._snapshot("Незавершённый ордер блокирует торговлю; требуется сверка")
            raise RuntimeError("Незавершённый ордер: автоматическая торговля заблокирована до ручной сверки с биржей")
        price = self.exchange.price(self.symbol)
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Некорректная котировка")
        self.store.set("quote", {"symbol": self.symbol, "price": price, "time": self.clock()})
        position = self.store.get("position")
        if self.market == "futures" and position:
            sign = 1 if position["direction"] == "LONG" else -1
            movement = sign * (price / position["price"] - 1) * 100
            equity = position["margin"] - position["entry_fee"] + sign * (price-position["price"]) * position["quantity"]
            costs = self.cfg.paper_costs()
            liquidation_fill = costs.fill(price, position["direction"], opening=False)
            maintenance = position["quantity"] * price * 0.005 + position["quantity"] * liquidation_fill * costs.exit_fee_bps / 10000 + abs(liquidation_fill-price)*position["quantity"]
            reason = ("Модельная ликвидация" if equity <= maintenance else
                      "Сработал стоп-лосс" if movement <= -self.cfg.stop else
                      "Сработал тейк-профит" if movement >= self.cfg.take else None)
            if reason:
                self._snapshot(reason, action="CLOSE", candle=self.store.get("candle"))
                self.futures_trade("CLOSE", price, self.store.get("candle"), reason)
                self._snapshot(reason + "; позиция закрыта", action="CLOSE")
                return
        if self.market == "spot" and position and (price <= position["price"] * (1-self.cfg.stop/100) or price >= position["price"] * (1+self.cfg.take/100)):
            reason = "Сработал стоп-лосс" if price <= position["price"] * (1-self.cfg.stop/100) else "Сработал тейк-профит"
            self._snapshot(reason + "; отправка продажи", action="SELL")
            self.trade("SELL", price, self.store.get("candle"))
            self._snapshot(reason + "; позиция закрыта", action="SELL")
            return
        candles = self.exchange.candles(self.symbol)
        if not candles:
            self._snapshot("Нет закрытых свечей для расчёта", count=0)
            return
        if candles[-1][0] == self.store.get("candle"):
            snapshot = self.store.get("signal_snapshot")
            if snapshot:
                snapshot.pop("check_error", None)
                snapshot["checked_at"] = self.clock()
                self.store.set("signal_snapshot", snapshot)
            else:
                self._snapshot("Свеча уже обработана ранее; повторного входа нет",
                               candle=candles[-1][0], **signal_details([p for _, p in candles]))
            return
        stamp, _ = candles[-1]
        details = signal_details([p for _, p in candles])
        action = self.resolve_action(details, stamp, position)
        if action != details["action"]:
            details = {**details, "raw_action": details["action"], "action": action}
        reason = "Нового пересечения SMA 10/30 нет"
        if details["count"] < 31:
            reason = "Недостаточно закрытых свечей: нужно не менее 31"
        self._snapshot("Проверка условий сделки", candle=stamp, **details)
        if self.market == "futures":
            reason = self.futures_signal(action, price, stamp, position, [p for _, p in candles]) if action != "HOLD" else reason
        elif action == "SELL" and position:
            self.trade("SELL", price, stamp)
            reason = "Пересечение вниз; позиция закрыта"
        elif action == "SELL":
            reason = "Пересечение вниз, но позиции нет; шорт на споте не открывается"
        elif action == "BUY":
            if position:
                reason = "Пересечение вверх, но позиция уже открыта"
            elif self.store.get("paused", True):
                reason = "Пересечение вверх пропущено: включена пауза"
            elif self.store.loss() >= self.cfg.daily_loss:
                reason = "Пересечение вверх пропущено: достигнут дневной лимит убытка"
            elif not ai_allows(self.symbol, [p for _, p in candles]):
                reason = "Пересечение вверх пропущено: ИИ-фильтр отклонил вход или недоступен"
            else:
                self.trade("BUY", price, stamp)
                reason = "Пересечение вверх; позиция открыта"
        self._snapshot(reason, candle=stamp, **details)
        self.store.set("candle", stamp)

    def futures_signal(self, action, price, stamp, position, prices):
        direction = "LONG" if action == "BUY" else "SHORT"
        if position:
            if position["direction"] == direction:
                return f"Позиция {direction} уже открыта"
            self.futures_trade("CLOSE", price, stamp, "Обратное пересечение")
            return "Обратное пересечение; позиция закрыта. Новый вход — по следующему пересечению"
        if self.store.get("paused", True):
            return f"Вход {direction} пропущен: включена пауза"
        if self.store.loss() >= self.cfg.daily_loss:
            return f"Вход {direction} пропущен: достигнут дневной лимит убытка"
        if self.store.get("futures_balance", 1000.0) < self.cfg.amount:
            return "Недостаточно виртуального баланса для маржи"
        if not ai_allows(self.symbol, prices, direction=direction, market="futures simulation on spot prices"):
            return "ИИ-фильтр отклонил вход или недоступен"
        self.futures_trade(direction, price, stamp)
        return f"Открыта виртуальная позиция {direction}, плечо {self.leverage}×"
