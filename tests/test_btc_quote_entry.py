from unittest import TestCase

import pandas as pd

from research import btc_quote_entry as study


class EntryQuoteTests(TestCase):
    def test_reprices_both_sides_from_last_received_quote(self):
        quotes = pd.DataFrame([
            ("binance-futures", "BTCUSDT", 1_000_000, 1_010_000, 89_999., 90_001., .01, .01),
            ("binance-futures", "BTCUSDT", 1_400_000, 2_100_000, 92_000., 92_001., .01, .01),
        ], columns=["exchange", "symbol", "timestamp", "local_timestamp", "bid_price", "ask_price",
                    "bid_amount", "ask_amount"])
        trades = pd.DataFrame(dict(side=[1, -1, 1], entry_ms=[2000, 2000, 7500],
                                   ts=[0, 0, 0], scenario=[0, 1, 0], q=[.001]*3,
                                   entry=[90_045., 89_910., 90_045.]))
        long, short, stale = study.reprice(trades, quotes)
        self.assertEqual(long["quote_entry"], 90_001)
        self.assertEqual(short["quote_entry"], 89_999)
        self.assertAlmostEqual(long["entry_savings_usdt"], .044)
        self.assertAlmostEqual(short["entry_savings_usdt"], .089)
        self.assertEqual(stale["reject"], "quote_time")
