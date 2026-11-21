from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import pandas as pd

from research import btc_quote_archive as study


class QuoteArchiveTests(TestCase):
    def test_uses_received_quote_only_and_rejects_stale_update(self):
        start = pd.Timestamp("2025-02-01", tz="UTC").value // 1000
        frame = pd.DataFrame([
            ["binance-futures", "BTCUSDT", start + 58*study.SECOND, start + 59*study.SECOND,
             .001, 90_001, 90_000, .002],
            ["binance-futures", "BTCUSDT", start + 59*study.SECOND, start + 61*study.SECOND,
             .002, 100_100, 100_099, .001],
        ], columns=["exchange", "symbol", "timestamp", "local_timestamp",
                    "ask_amount", "ask_price", "bid_price", "bid_amount"])
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "quotes.csv"
            frame.to_csv(path, index=False)
            summary, rows = study.sample(path, "2025-02-01")
        at_boundary, after_one, after_six = rows[:3]
        self.assertFalse(at_boundary["available"])
        self.assertEqual(after_one["ask"], 90_001)
        self.assertTrue(after_one["long_eligible"])
        self.assertFalse(after_six["available"])
        self.assertEqual(summary["valid_rows"], 2)
