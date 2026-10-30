import tempfile
import sqlite3
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from trading_bot.core import Config, Engine, Store
from trading_bot.__main__ import Telegram


class Prices:
    def price(self, symbol):
        return 100.0

    def candles(self, symbol):
        return [(n * 60000, p) for n, p in enumerate([100.] * 30 + [90.])]

    def symbol(self, symbol):
        return {"status": "TRADING"}

    def call(self, *args, **kwargs):
        raise AssertionError("Simulator must never submit exchange requests")


class FuturesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "bot.db")
        self.store = Store(self.path)
        self.addCleanup(self.store.db.close)
        self.engine = Engine(Config(), self.store, Prices())
        self.engine.change_market("futures")
        self.engine.change_leverage(10)
        self.store.set("paused", False)

    def test_long_pnl_and_margin_reservation(self):
        self.engine.futures_trade("LONG", 100, 1)
        pos = self.store.get("position")
        self.assertEqual(pos["quantity"], 1)
        self.assertEqual(pos["margin"], 10)
        self.assertEqual(pos["cost"], 100)
        self.assertEqual(self.store.get("futures_balance"), 990)
        self.engine.close_position(102, 2)
        self.assertAlmostEqual(self.store.history()[0][5], 1.798)
        self.assertAlmostEqual(self.store.get("futures_balance"), 1001.798)
        self.assertIsNone(self.store.get("position"))

    def test_short_pnl_both_directions(self):
        for exit_price, expected in ((98, 1.802), (102, -2.202)):
            with self.subTest(exit_price=exit_price):
                self.engine.futures_trade("SHORT", 100, 1)
                self.engine.close_position(exit_price, 2)
                self.assertAlmostEqual(self.store.history()[0][5], expected)

    def test_all_integer_leverages_scale_quantity(self):
        for leverage in range(1, 11):
            self.engine.change_leverage(leverage)
            self.store.set("paused", False)
            self.engine.futures_trade("LONG", 100, leverage)
            self.assertAlmostEqual(self.store.get("position")["quantity"], 0.1 * leverage)
            self.engine.close_position(100, leverage)

    def test_leverage_validation_and_spot_restriction(self):
        for invalid in (0, 11, -1, 2.5, True, "10"):
            with self.assertRaises(ValueError):
                self.engine.change_leverage(invalid)
        self.engine.change_market("spot")
        with self.assertRaises(ValueError):
            self.engine.change_leverage(2)
        self.assertEqual(self.engine.market, "spot")

    def test_settings_blocked_with_position_or_pending(self):
        self.engine.futures_trade("SHORT", 100, 1)
        for change in (lambda: self.engine.change_market("spot"), lambda: self.engine.change_leverage(1), lambda: self.engine.change_symbol("ETHUSDT")):
            with self.assertRaises(ValueError):
                change()
        self.engine.close_position(100)
        self.store.set("pending", {"id": "uncertain"})
        with self.assertRaises(ValueError):
            self.engine.change_market("spot")
        with self.assertRaises(ValueError):
            self.engine.change_leverage(1)

    def test_switching_pauses_and_persists_across_restart(self):
        self.engine.change_leverage(7)
        self.assertTrue(self.store.get("paused"))
        restored = Engine(Config(), self.store, Prices())
        self.assertEqual(restored.market, "futures")
        self.assertEqual(restored.leverage, 7)
        self.store.set("paused", False)
        restored.futures_trade("SHORT", 100, 1)
        reopened = Store(self.path)
        try:
            restarted = Engine(Config(), reopened, Prices())
            restarted.close_position(99, 2)
            self.assertAlmostEqual(reopened.get("futures_balance"), 1000.5607)
        finally:
            reopened.db.close()

    def test_down_cross_opens_short_once(self):
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.store.get("position")["direction"], "SHORT")
        self.assertEqual(len(self.store.history()), 1)
        self.assertIn("SHORT", self.store.get("signal_snapshot")["reason"])

    def test_up_cross_opens_long(self):
        with patch.object(self.engine.exchange, "candles", return_value=[(n*60000,p) for n,p in enumerate([100.]*30+[110.])]):
            self.engine.tick()
        self.assertEqual(self.store.get("position")["direction"], "LONG")

    def test_opposite_signal_closes_without_same_candle_reversal(self):
        self.engine.futures_trade("LONG", 100, 1)
        self.engine.tick()
        self.engine.tick()
        self.assertIsNone(self.store.get("position"))
        self.assertEqual(len(self.store.history()), 2)
        self.assertIn("Обратное пересечение", self.store.get("signal_snapshot")["reason"])

    def test_short_stop_and_take_while_paused(self):
        for price, word in ((103, "стоп-лосс"), (95, "тейк-профит")):
            self.store.set("paused", False)
            self.engine.futures_trade("SHORT", 100, 1)
            self.store.set("paused", True)
            with patch.object(self.engine.exchange, "price", return_value=price):
                self.engine.tick()
            self.assertIsNone(self.store.get("position"))
            self.assertIn(word, self.store.get("signal_snapshot")["reason"])

    def test_liquidation_and_gap_loss_capped_at_margin(self):
        for direction, price in (("LONG", 50), ("SHORT", 200)):
            self.store.set("paused", False)
            self.engine.cfg.daily_loss = 100
            self.engine.futures_trade(direction, 100, 1)
            with patch.object(self.engine.exchange, "price", return_value=price):
                self.engine.tick()
            self.assertIn("ликвидация", self.store.get("signal_snapshot")["reason"])
            self.assertEqual(self.store.history()[0][5], -10)
        self.assertEqual(self.store.get("futures_balance"), 980)

    def test_pause_loss_limit_and_insufficient_balance_block_entries(self):
        self.store.set("paused", True)
        self.assertFalse(self.engine.futures_trade("LONG", 100, 1))
        self.store.set("paused", False)
        with patch.object(self.store, "loss", return_value=5):
            self.assertFalse(self.engine.futures_trade("SHORT", 100, 1))
        self.store.set("futures_balance", 9)
        self.engine.tick()
        self.assertIsNone(self.store.get("position"))
        self.assertIn("баланса", self.store.get("signal_snapshot")["reason"])

    def test_testnet_futures_and_spot_execution_blocked(self):
        with self.assertRaises(ValueError):
            self.engine.trade("BUY", 100, 1)
        self.engine.cfg.mode = "testnet"
        with self.assertRaises(ValueError):
            self.engine.futures_trade("LONG", 100, 1)
        with self.assertRaises(ValueError):
            self.engine.change_market("futures")

    def test_telegram_commands_history_and_close_short(self):
        bot = Telegram(self.engine)
        self.assertIn("10×", bot.command("/leverage"))
        self.assertIn("целое", bot.command("/leverage 11"))
        self.assertIn("3×", bot.command("/leverage 3"))
        self.store.set("paused", False)
        self.engine.futures_trade("SHORT", 100, 1)
        self.assertIn("Сначала закройте", bot.command("/mode spot"))
        self.assertIn("Симуляция фьючерсов", bot.command("/status"))
        self.assertIn("OPEN SHORT", bot.command("/history"))
        self.assertIn("закрыта", bot.command("/close"))
        self.assertIn("CLOSE SHORT", bot.command("/history"))
        self.assertTrue(self.store.get("paused"))

    def test_old_database_keeps_spot_position_and_history(self):
        self.engine.change_market("spot")
        self.engine.trade("BUY", 100, 1)
        restored = Engine(Config(), self.store, Prices())
        self.assertEqual(restored.market, "spot")
        restored.close_position(101, 2)
        self.assertEqual(len(self.store.history()), 2)
        self.assertEqual(self.store.history()[0][2], "SELL")

    def test_nonfinite_prices_rejected(self):
        for price in (float("nan"), float("inf"), -1, 0):
            with self.assertRaises(ValueError):
                self.engine.futures_trade("LONG", price, 1)

    def test_migration_from_original_database(self):
        old_path = str(Path(self.tmp.name) / "old.db")
        with sqlite3.connect(old_path) as db:
            db.execute("CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE trades (ts REAL, symbol TEXT, side TEXT, quantity REAL, price REAL, pnl REAL, mode TEXT)")
            db.execute("INSERT INTO state VALUES (?,?)", ("position", json.dumps({"price": 100, "quantity": 0.1, "cost": 10})))
            db.execute("INSERT INTO trades VALUES (1,'BTCUSDT','BUY',0.1,100,0,'paper')")
        migrated = Store(old_path)
        try:
            engine = Engine(Config(), migrated, Prices())
            self.assertEqual(engine.market, "spot")
            self.assertEqual(engine.leverage, 1)
            with self.assertRaises(ValueError):
                engine.change_market("futures")
            engine.close_position(101)
            self.assertEqual(len(migrated.history()), 2)
        finally:
            migrated.db.close()

    def test_futures_losses_count_in_shared_daily_limit(self):
        self.engine.futures_trade("LONG", 100, 1)
        self.engine.close_position(94, 2)
        self.assertGreater(self.store.loss(), 5)
        self.engine.change_market("spot")
        self.engine.trade("BUY", 100, 3)
        self.assertIsNone(self.store.get("position"))


if __name__ == "__main__":
    unittest.main()
