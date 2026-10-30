import hashlib
import hmac
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


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

    @classmethod
    def load(cls):
        cfg = cls(os.getenv("BOT_MODE", "paper"), os.getenv("SYMBOL", "BTCUSDT"),
                  float(os.getenv("TRADE_USDT", "10")), float(os.getenv("MAX_DAILY_LOSS_USDT", "5")),
                  float(os.getenv("STOP_LOSS_PCT", "2")), float(os.getenv("TAKE_PROFIT_PCT", "4")),
                  os.getenv("DB_PATH", "data/bot.db"))
        if cfg.mode not in {"paper", "testnet"}:
            raise ValueError("Поддерживаются только paper и testnet; реальная торговля отключена")
        if cfg.amount <= 0 or cfg.daily_loss <= 0 or not 0 < cfg.stop < 100 or cfg.take <= 0:
            raise ValueError("Некорректные лимиты риска")
        return cfg


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS trades (ts REAL, symbol TEXT, side TEXT, quantity REAL, price REAL, pnl REAL, mode TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY, text TEXT NOT NULL)")
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def loss(self):
        start = int(time.time() // 86400) * 86400
        return -self.db.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE ts>=?", (start,)).fetchone()[0]

    def history(self, limit=10):
        return self.db.execute(
            "SELECT ts,symbol,side,quantity,price,pnl,mode FROM trades ORDER BY rowid DESC LIMIT ?",
            (limit,)).fetchall()

    def report_error(self, stage, exc):
        # Do not persist exception bodies or URLs: they can contain API tokens.
        code = getattr(exc, "code", None)
        reason = type(exc).__name__ + (f" HTTP {code}" if isinstance(code, int) else "")
        now = time.time()
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


def signal(prices):
    if len(prices) < 31:
        return "HOLD"
    old_fast, old_slow = sum(prices[-11:-1]) / 10, sum(prices[-31:-1]) / 30
    fast, slow = sum(prices[-10:]) / 10, sum(prices[-30:]) / 30
    if old_fast <= old_slow and fast > slow:
        return "BUY"
    if old_fast >= old_slow and fast < slow:
        return "SELL"
    return "HOLD"


def ai_allows(symbol, prices):
    if os.getenv("AI_ENABLED", "false").lower() != "true":
        return True
    key, model = os.getenv("GEMINI_API_KEY"), os.getenv("GEMINI_MODEL")
    if not key or not model:
        return False
    try:
        result = request("https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent",
                         {"contents": [{"parts": [{"text": "Evaluate this experimental spot BUY. Return JSON {\"allow\":true|false}. Symbol: " + symbol + "; closed minute prices: " + json.dumps(prices)}]}],
                          "generationConfig": {"responseMimeType": "application/json", "temperature": 0}},
                         {"x-goog-api-key": key})
        decision = json.loads(result["candidates"][0]["content"]["parts"][0]["text"])
        return decision.get("allow") is True
    except Exception:
        return False


class Engine:
    def __init__(self, cfg, store, exchange):
        self.cfg, self.store, self.exchange = cfg, store, exchange
        identity = {"mode": cfg.mode, "amount": cfg.amount}
        previous = store.get("identity")
        if previous and previous != identity:
            raise ValueError("Для другого режима/размера сделки используйте отдельную базу DB_PATH")
        store.set("identity", identity)
        self.symbol = store.get("symbol", cfg.symbol)

    def change_symbol(self, symbol):
        if self.store.get("position") or self.store.get("pending"):
            raise ValueError("Сначала закройте позицию и разрешите незавершённый ордер")
        self.exchange.symbol(symbol)
        self.symbol = symbol
        self.store.set("symbol", symbol)
        self.store.set("candle", None)
        self.store.set("quote", None)

    def trade(self, side, price, candle):
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
            self.store.db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?)", (time.time(), self.symbol, side, quantity, fill, pnl, self.cfg.mode))
            for key, value in (("position", new_position), ("pending", None), ("candle", candle)):
                self.store.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))
            label = "Виртуальная сделка" if self.cfg.mode == "paper" else "Сделка Testnet"
            action = "Покупка" if side == "BUY" else "Продажа"
            notice = f"{label}: {action} {self.symbol}\nКоличество: {quantity:.8f}\nЦена: {fill:.8f} USDT"
            if side == "SELL":
                notice += f"\nРасчётный результат с комиссией: {pnl:+.4f} USDT"
            self.store.db.execute("INSERT INTO notifications(text) VALUES (?)", (notice,))

    def tick(self):
        if self.store.get("pending"):
            raise RuntimeError("Незавершённый ордер: автоматическая торговля заблокирована до ручной сверки с биржей")
        price = self.exchange.price(self.symbol)
        self.store.set("quote", {"symbol": self.symbol, "price": price, "time": time.time()})
        position = self.store.get("position")
        if position and (price <= position["price"] * (1-self.cfg.stop/100) or price >= position["price"] * (1+self.cfg.take/100)):
            self.trade("SELL", price, self.store.get("candle"))
            return
        candles = self.exchange.candles(self.symbol)
        if not candles or candles[-1][0] == self.store.get("candle"):
            return
        stamp, _ = candles[-1]
        action = signal([p for _, p in candles])
        if action == "SELL" and position:
            self.trade("SELL", price, stamp)
        elif action == "BUY" and not self.store.get("paused", True) and ai_allows(self.symbol, [p for _, p in candles]):
            self.trade("BUY", price, stamp)
        self.store.set("candle", stamp)
