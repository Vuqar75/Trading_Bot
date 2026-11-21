from unittest import TestCase
from unittest.mock import patch

import numpy as np

from research import eth_market_regimes as study


class MarketRegimeTests(TestCase):
    def test_up_down_and_range_use_only_closed_prices(self):
        start = study.core.stamp("2024-01-01")
        count = study.daily.DAY_MIN
        time = start + 92 * study.daily.DAY
        for close, direction, mode in (
            (120, "LONG", "up"),
            (80, "SHORT", "down"),
            (91, "LONG", "range"),
            (109, "SHORT", "range"),
        ):
            with self.subTest(close=close):
                trade = np.tile([100., 110., 90., 100.], (93 * count, 1))
                if mode == "range":
                    trade[2 * count - 1, 3] = close
                trade[92 * count - 1] = [close, close + 2, close - 2, close]
                trade[92 * count] = [1000, 1001, 999, 1000]
                with patch.object(study.core, "START", start):
                    result = study.signals(trade, time, time + study.daily.DAY)
                (signal,) = result["combined"]
                self.assertEqual((signal["direction"], signal["regime"]), (direction, mode))
                self.assertEqual(bool(result["range"]), mode == "range")
                self.assertEqual(bool(result["both"]), mode != "range")
