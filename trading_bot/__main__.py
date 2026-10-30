import logging
import os
import time
from urllib.error import HTTPError

from .core import Binance, Config, Engine, Store, request

log = logging.getLogger("bot")


class Telegram:
    def __init__(self, engine):
        self.engine = engine
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.owner = os.getenv("TELEGRAM_OWNER_ID")
        self.offset = engine.store.get("telegram_offset", 0)

    def send(self, text):
        request(f"https://api.telegram.org/bot{self.token}/sendMessage",
                {"chat_id": self.owner, "text": text})

    def command(self, text):
        parts = text.split()
        cmd = parts[0].split("@")[0] if parts else ""
        store = self.engine.store
        if cmd in {"/start", "/help"}:
            return "/status — состояние\n/resume — разрешить входы\n/pause — запретить входы\n/stop — запретить входы, продолжить защиту позиции\n/close — закрыть позицию\n/pair BTCUSDT — выбрать пару\n/mode — вид торговли"
        if cmd == "/status":
            return f"Режим: {self.engine.cfg.mode}, Spot\nПара: {self.engine.symbol}\nПауза: {store.get('paused', True)}\nПозиция: {store.get('position')}\nНезавершённый ордер: {bool(store.get('pending'))}\nУбыток за UTC-день: {store.loss():.4f} USDT"
        if cmd in {"/pause", "/stop", "/resume"}:
            if cmd == "/resume" and store.get("pending"):
                return "Сначала требуется сверка незавершённого ордера"
            store.set("paused", cmd != "/resume")
            return "Новые входы разрешены" if cmd == "/resume" else "Новые входы запрещены. Контроль открытой позиции продолжается."
        if cmd == "/pair" and len(parts) == 2:
            self.engine.change_symbol(parts[1].upper())
            return "Пара выбрана: " + self.engine.symbol
        if cmd == "/mode":
            return "Доступен Spot. Фьючерсы и маржинальная торговля ещё не реализованы. Режим paper/testnet задаётся при запуске."
        if cmd == "/close":
            store.set("paused", True)
            if store.get("pending"):
                return "Закрытие заблокировано: требуется сверка ордера"
            if not store.get("position"):
                return "Открытой позиции нет"
            self.engine.trade("SELL", self.engine.exchange.price(self.engine.symbol), store.get("candle"))
            return "Позиция закрыта, новые входы запрещены"
        return "Неизвестная команда. /help"

    def poll(self):
        if not self.token or not self.owner:
            return
        updates = request(f"https://api.telegram.org/bot{self.token}/getUpdates",
                          {"offset": self.offset, "timeout": 0, "allowed_updates": ["message"]})
        if not updates.get("ok"):
            raise RuntimeError("Telegram вернул ошибку")
        for update in updates["result"]:
            msg = update.get("message", {})
            # Only private messages from the configured owner are accepted.
            if str(msg.get("from", {}).get("id")) == self.owner and str(msg.get("chat", {}).get("id")) == self.owner:
                try:
                    reply = self.command(msg.get("text", ""))
                except Exception as exc:
                    reply = "Команда не выполнена: " + type(exc).__name__ + ". Проверьте состояние /status."
                self.send(reply)
            self.offset = update["update_id"] + 1
            self.engine.store.set("telegram_offset", self.offset)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.load()
    store = Store(cfg.db)
    # Single process lock prevents two bots from submitting the same strategy.
    import fcntl
    lock = open(cfg.db + ".lock", "a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    exchange = Binance()
    engine = Engine(cfg, store, exchange)
    exchange.symbol(engine.symbol)
    if cfg.mode == "testnet":
        exchange.call("/api/v3/account", signed=True)
    telegram = Telegram(engine)
    interval = max(5, int(os.getenv("POLL_SECONDS", "60")))
    log.info("Запуск: %s Spot %s. Пауза: %s", cfg.mode, engine.symbol, store.get("paused", True))
    last_tick = 0
    while True:
        try:
            telegram.poll()
        except Exception as exc:
            log.error("Telegram: %s", type(exc).__name__)
        if time.monotonic() - last_tick >= interval:
            try:
                engine.tick()
            except Exception as exc:
                # Never log request URLs or exception bodies: they may contain secrets.
                store.set("paused", True)
                log.error("Торговля приостановлена: %s", type(exc).__name__)
            last_tick = time.monotonic()
        time.sleep(2)


if __name__ == "__main__":
    main()
