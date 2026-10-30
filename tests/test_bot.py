import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trading_bot.core import Config, Engine, Store, ai_allows, signal
from trading_bot.__main__ import Telegram


class Exchange:
    def price(self, symbol):
        return 100

    def symbol(self, symbol):
        if symbol != "BTCUSDT":
            raise ValueError("Unsupported")

    def candles(self, symbol):
        return list(enumerate([100.] * 30 + [110.]))


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "bot.db"))
        self.addCleanup(self.store.db.close)
        self.engine = Engine(Config(), self.store, Exchange())

    def test_crossovers(self):
        self.assertEqual(signal([100.] * 30 + [110.]), "BUY")
        self.assertEqual(signal([100.] * 30 + [90.]), "SELL")
        self.assertEqual(signal([100.] * 31), "HOLD")

    def test_default_pause_then_one_entry_per_candle(self):
        self.engine.tick()
        self.assertIsNone(self.store.get("position"))
        self.store.set("paused", False)
        self.store.set("candle", None)
        self.engine.tick()
        self.engine.tick()
        self.assertIsNotNone(self.store.get("position"))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)

    def test_stop_works_while_paused_and_persists(self):
        self.engine.trade("BUY", 110, 1)
        self.engine.tick()
        self.assertIsNone(self.store.get("position"))
        self.assertGreater(self.store.loss(), 0)
        restored = Engine(Config(), self.store, Exchange())
        self.assertIsNone(restored.store.get("position"))

    def test_loss_limit_blocks_buy(self):
        self.engine.cfg.daily_loss = 0.01
        self.engine.trade("BUY", 110, 1)
        self.engine.trade("SELL", 100, 2)
        self.engine.trade("BUY", 100, 3)
        self.assertIsNone(self.store.get("position"))

    def test_pending_blocks_trading_and_pair_change(self):
        self.store.set("pending", {"client_id": "uncertain"})
        with self.assertRaises(RuntimeError):
            self.engine.tick()
        with self.assertRaises(ValueError):
            self.engine.change_symbol("BTCUSDT")

    def test_position_blocks_pair_change(self):
        self.engine.trade("BUY", 100, 1)
        with self.assertRaises(ValueError):
            self.engine.change_symbol("BTCUSDT")

    def test_ai_failure_rejects_entry(self):
        with patch.dict("os.environ", {"AI_ENABLED": "true", "GEMINI_API_KEY": "", "GEMINI_MODEL": ""}):
            self.assertFalse(ai_allows("BTCUSDT", [100]))

    def test_live_mode_rejected(self):
        with patch.dict("os.environ", {"BOT_MODE": "live"}):
            with self.assertRaises(ValueError):
                Config.load()

    def test_telegram_stop_keeps_position(self):
        self.engine.trade("BUY", 100, 1)
        Telegram(self.engine).command("/stop")
        self.assertTrue(self.store.get("paused"))
        self.assertIsNotNone(self.store.get("position"))

    def test_ambiguous_submission_is_not_retried(self):
        self.engine.cfg.mode = "testnet"
        with patch.object(self.engine.exchange, "call", side_effect=TimeoutError, create=True) as call:
            with self.assertRaises(TimeoutError):
                self.engine.trade("BUY", 100, 1)
            self.assertIsNotNone(self.store.get("pending"))
            with self.assertRaises(RuntimeError):
                self.engine.tick()
            self.assertEqual(call.call_count, 1)

    def test_telegram_ignores_other_users(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_OWNER_ID": "123"}):
            telegram = Telegram(self.engine)
        updates = {"ok": True, "result": [{"update_id": 1, "message": {
            "from": {"id": 999}, "chat": {"id": 123}, "text": "/resume"}}]}
        with patch("trading_bot.__main__.request", return_value=updates), patch.object(telegram, "send") as send:
            telegram.poll()
            send.assert_not_called()
        self.assertTrue(self.store.get("paused", True))

    def test_trade_and_notification_commit_together(self):
        self.engine.trade("BUY", 100, 1)
        self.engine.trade("SELL", 110, 2)
        rows = self.store.db.execute("SELECT text FROM notifications ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertIn("Виртуальная сделка: Покупка BTCUSDT", rows[0][0])
        self.assertIn("Продажа BTCUSDT", rows[1][0])
        self.assertIn("+0.9790", rows[1][0])
        self.assertEqual(self.store.history()[0][2], "SELL")

    def test_failed_delivery_survives_reopen(self):
        self.engine.trade("BUY", 100, 1)
        telegram = Telegram(self.engine)
        telegram.token, telegram.owner = "test", "123"
        with patch.object(telegram, "send", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                telegram.flush_notifications()
        reopened = Store(str(Path(self.tmp.name) / "bot.db"))
        try:
            self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1)
        finally:
            reopened.db.close()
        with patch.object(telegram, "send") as send:
            telegram.flush_notifications()
            telegram.flush_notifications()
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)

    def test_error_notices_deduplicated_and_redacted(self):
        with patch("trading_bot.core.time.time", return_value=1000):
            self.store.report_error("торговля", RuntimeError("secret token URL"))
            self.store.report_error("торговля", RuntimeError("secret token URL"))
        rows = self.store.db.execute("SELECT text FROM notifications").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("secret", rows[0][0])
        self.assertNotIn("secret", str(self.store.get("last_error")))
        with patch("trading_bot.core.time.time", return_value=1901):
            self.store.report_error("торговля", RuntimeError("secret"))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 2)

    def test_status_quote_and_staleness(self):
        telegram = Telegram(self.engine)
        self.assertIn("ещё не получена", telegram.command("/status"))
        with patch("trading_bot.core.time.time", return_value=1000):
            self.engine.tick()
        with patch("trading_bot.__main__.time.time", return_value=1001):
            status = telegram.command("/status")
            self.assertIn("100.00000000 USDT", status)
            self.assertIn("Баку", status)
            self.assertNotIn("УСТАРЕЛА", status)
        with patch("trading_bot.__main__.time.time", return_value=1200):
            self.assertIn("УСТАРЕЛА", telegram.command("/status"))

    def test_empty_and_populated_history(self):
        telegram = Telegram(self.engine)
        self.assertIn("пуста", telegram.command("/history"))
        self.engine.trade("BUY", 100, 1)
        self.engine.trade("SELL", 110, 2)
        history = telegram.command("/history")
        self.assertLess(history.index("Продажа"), history.index("Покупка"))
        self.assertIn("+0.9790", history)

    def test_history_bounded_and_pair_clears_quote(self):
        for n in range(12):
            self.engine.trade("BUY" if n % 2 == 0 else "SELL", 100, n)
        self.assertEqual(len(self.store.history()), 10)
        self.store.set("quote", {"symbol": "BTCUSDT", "price": 100, "time": 1})
        self.engine.change_symbol("BTCUSDT")
        self.assertIsNone(self.store.get("quote"))

    def test_api_rejected_notification_is_kept(self):
        self.engine.trade("BUY", 100, 1)
        telegram = Telegram(self.engine)
        telegram.token, telegram.owner = "test", "123"
        with patch("trading_bot.__main__.request", return_value={"ok": False}):
            with self.assertRaises(RuntimeError):
                telegram.flush_notifications()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
