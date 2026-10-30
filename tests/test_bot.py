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


if __name__ == "__main__":
    unittest.main()
