from unittest import TestCase

import pandas as pd

from research.btc_quote_full_replay import Quotes, SECOND


class FullReplayQuoteTests(TestCase):
    def setUp(self):
        self.quotes = Quotes(pd.DataFrame([
            ("binance-futures", "BTCUSDT", 900000, 1000000, 100., 100.1, .1, .1),
            ("binance-futures", "BTCUSDT", 1100000, 1200000, 98., 98.1, .1, .1),
            ("binance-futures", "BTCUSDT", 1200000, 1300000, 97., 97.1, .1, .1),
        ], columns=["exchange", "symbol", "timestamp", "local_timestamp", "bid_price", "ask_price",
                    "bid_amount", "ask_amount"]))

    def test_long_stop_uses_bid_and_price_after_execution_latency(self):
        t, reason = self.quotes.trigger(SECOND, 2*SECOND, 1, 99, 105)
        self.assertEqual((t, reason), (1200000, "SL"))
        self.assertEqual(self.quotes.at(t+100000, -1, .001), 97.)
        self.assertEqual(self.quotes.at(t-1, -1, .001), 100.)

    def test_short_target_uses_ask_and_rejects_stale_fill(self):
        t, reason = self.quotes.trigger(SECOND, 2*SECOND, -1, 101, 98.05)
        self.assertEqual((t, reason), (1300000, "TP"))
        self.assertIsNone(self.quotes.at(7*SECOND, 1, .001))
