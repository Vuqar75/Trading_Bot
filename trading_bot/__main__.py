import logging
import os
import time
from datetime import datetime, timedelta, timezone

from .core import Binance, Config, Engine, Store, request

log = logging.getLogger("bot")


def local_time(stamp):
    return datetime.fromtimestamp(stamp, timezone(timedelta(hours=4))).strftime("%d.%m %H:%M:%S") + " (Баку)"


class Telegram:
    def __init__(self, engine):
        self.engine = engine
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.owner = os.getenv("TELEGRAM_OWNER_ID")
        self.offset = engine.store.get("telegram_offset", 0)

    def send(self, text):
        result = request(f"https://api.telegram.org/bot{self.token}/sendMessage",
                {"chat_id": self.owner, "text": text})
        if not result.get("ok"):
            raise RuntimeError("Telegram не подтвердил доставку")

    def flush_notifications(self):
        if not self.token or not self.owner:
            return
        for identifier, text in self.engine.store.db.execute(
                "SELECT id,text FROM notifications ORDER BY id LIMIT 5").fetchall():
            self.send(text)
            with self.engine.store.db:
                self.engine.store.db.execute("DELETE FROM notifications WHERE id=?", (identifier,))

    def command(self, text):
        parts = text.split()
        cmd = parts[0].split("@")[0] if parts else ""
        store = self.engine.store
        if cmd in {"/start", "/help"}:
            return "/status — состояние и котировка\n/signal — средние и причина решения\n/history — последние 10 сделок\n/resume — разрешить входы\n/pause — запретить входы\n/stop — запретить входы, продолжить защиту позиции\n/close — закрыть позицию\n/pair BTCUSDT — выбрать пару\n/mode — вид торговли"
        if cmd == "/signal":
            snapshot = store.get("signal_snapshot")
            if not snapshot or snapshot["symbol"] != self.engine.symbol:
                return "Диагностика ещё не получена. Дождитесь следующей проверки стратегии."
            age = max(0, int(time.time() - snapshot["time"]))
            lines = [f"Источник: {snapshot['source']} (testnet.binance.vision)",
                     f"Пара: {snapshot['symbol']} | Таймфрейм: {snapshot['interval']}",
                     f"Решение: {local_time(snapshot['time'])}, {age} сек. назад",
                     f"Пауза сейчас: {store.get('paused', True)}"]
            if "candle" in snapshot:
                opened = snapshot["candle"] / 1000
                lines += [f"Закрытая свеча: {local_time(opened)} — {local_time(opened + 60)}",
                          f"Закрытых свечей в расчёте: {snapshot.get('count', 0)}"]
            if "fast" in snapshot:
                lines += [f"Предыдущая: SMA 10 = {snapshot['old_fast']:.8f}, SMA 30 = {snapshot['old_slow']:.8f}",
                          f"Последняя: SMA 10 = {snapshot['fast']:.8f}, SMA 30 = {snapshot['slow']:.8f}"]
            else:
                lines.append("Средние для этого решения не рассчитаны.")
            lines += [f"Сигнал: {snapshot.get('action', 'нет')}", f"Причина: {snapshot['reason']}"]
            if snapshot.get("check_error"):
                lines.append(f"Последняя проверка завершилась ошибкой: {snapshot['check_error']}. См. /status.")
            if age > max(120, 2 * max(5, int(os.getenv("POLL_SECONDS", "60")))):
                lines.append("Решение устарело. Проверьте /status и работу программы.")
            lines.append("Это сохранённое решение; /signal не запускает сделку. Основной Binance может иметь другие свечи.")
            return "\n".join(lines)
        if cmd == "/status":
            quote = store.get("quote")
            quote_text = "Котировка: ещё не получена"
            if quote and quote["symbol"] == self.engine.symbol:
                age = max(0, int(time.time() - quote["time"]))
                stale = age > max(120, 2 * max(5, int(os.getenv("POLL_SECONDS", "60"))))
                quote_text = (f"Котировка: {quote['price']:.8f} USDT\n"
                              f"Получена: {local_time(quote['time'])}, {age} сек. назад"
                              + (" — УСТАРЕЛА" if stale else ""))
            error = store.get("last_error")
            error_text = "Последняя ошибка: нет"
            if error:
                error_text = f"Последняя ошибка: {error['stage']}, {error['reason']}, {local_time(error['time'])}"
            return (f"Режим: {self.engine.cfg.mode}, Spot\nПара: {self.engine.symbol}\n"
                    f"Пауза: {store.get('paused', True)}\nПозиция: {store.get('position')}\n"
                    f"Незавершённый ордер: {bool(store.get('pending'))}\n"
                    f"Убыток за UTC-день: {store.loss():.4f} USDT\n{quote_text}\n{error_text}")
        if cmd == "/history":
            rows = store.history()
            if not rows:
                return "История сделок пуста. Бот ожидает сигнал стратегии."
            lines = ["Последние 10 сделок (время Баку):"]
            for stamp, symbol, side, quantity, price, pnl, mode in rows:
                action = "Покупка" if side == "BUY" else "Продажа"
                line = f"{local_time(stamp)} | {mode} | {action} {symbol}\n{quantity:.8f} × {price:.8f} USDT"
                if side == "SELL":
                    line += f" | Результат ≈ {pnl:+.4f} USDT"
                lines.append(line)
            return "\n\n".join(lines)
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
                    store = self.engine.store
                    store.report_error("команда", exc)
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
            store.report_error("Telegram", exc)
        if time.monotonic() - last_tick >= interval:
            try:
                engine.tick()
            except Exception as exc:
                # Never log request URLs or exception bodies: they may contain secrets.
                store.set("paused", True)
                store.report_error("торговля", exc)
                log.error("Торговля приостановлена: %s", type(exc).__name__)
            last_tick = time.monotonic()
        try:
            telegram.flush_notifications()
        except Exception as exc:
            log.error("Доставка уведомлений: %s", type(exc).__name__)
        time.sleep(2)


if __name__ == "__main__":
    main()
