from unittest import TestCase

import pandas as pd

from research import btc_bbo_imbalance as study


class ImbalanceTests(TestCase):
    def test_future_quote_does_not_change_signal(self):
        at = 100*study.SECOND
        ticks = list(range(41, 101)) + [101]
        quotes = study.Quotes(pd.DataFrame({
            "exchange": ["binance-futures"]*len(ticks), "symbol": ["BTCUSDT"]*len(ticks),
            "timestamp": [t*study.SECOND-100 for t in ticks],
            "local_timestamp": [t*study.SECOND for t in ticks],
            "ask_price": [100.1]*len(ticks), "bid_price": [100.]*len(ticks),
            "ask_amount": [1.]*60+[100.], "bid_amount": [4.]*61,
        }))
        self.assertAlmostEqual(study.imbalance(quotes, at), .6)
        self.assertIsNone(study.imbalance(quotes, 50*study.SECOND))
