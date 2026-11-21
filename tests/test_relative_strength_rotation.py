from unittest import TestCase
from unittest.mock import patch

import numpy as np

from research import relative_strength_rotation as study


class DailySelectorTests(TestCase):
    def test_closed_prices_choose_both_sides_and_ignore_next_minute(self):
        start = study.core.BEGIN - 8 * study.DAY
        closing_index = 8 * study.HOLD - 1
        markets = {}
        for symbol, prior, current in zip(study.SYMBOLS, (100, 120, 110), (110, 145, 90)):
            trade = np.full((10 * study.HOLD, 4), 90.)
            trade[closing_index - study.HOLD, 3] = prior
            trade[closing_index, 3] = current
            trade[closing_index + 1, 3] = 100_000
            trade[closing_index + study.HOLD, 3] = current * 0.7
            markets[symbol] = (trade, None, None)
        with patch.object(study.core, "START", start), patch.object(
            study.core, "END", study.core.BEGIN + 2 * study.DAY
        ):
            signals, missing = study.daily_signals(markets)
        self.assertEqual(missing, 0)
        self.assertEqual([(r["direction"], r["selected"]) for r in signals],
                         [("LONG", "ETHUSDT"), ("SHORT", "SOLUSDT")])

    def test_missing_price_blocks_both_rules(self):
        start = study.core.BEGIN - 8 * study.DAY
        trade = np.ones((9 * study.HOLD, 4))
        markets = {symbol: (trade.copy(), None, None) for symbol in study.SYMBOLS}
        markets["SOLUSDT"][0][8 * study.HOLD - 1, 3] = np.nan
        with patch.object(study.core, "START", start), patch.object(
            study.core, "END", study.core.BEGIN + study.DAY
        ):
            self.assertEqual(study.daily_signals(markets), ([], 1))
