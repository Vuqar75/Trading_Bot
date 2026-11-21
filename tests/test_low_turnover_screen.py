from unittest import TestCase
from unittest.mock import patch

import numpy as np

from research import low_turnover_screen as study


class LowTurnoverScreenTests(TestCase):
    def test_daily_breakout_excludes_signal_day_and_next_open(self):
        start = study.core.BEGIN - 32 * study.DAY
        trade = np.full((34 * study.DAY_MIN, 4), 100.)
        close = 32 * study.DAY_MIN - 1
        trade[close] = [103., 104., 102., 103.]
        trade[close + 1] = 1.
        with patch.object(study.core, "START", start):
            signals = study.daily_signals(trade, study.core.BEGIN, study.core.BEGIN + study.DAY)
        self.assertEqual(signals["channel_20"][0]["direction"], "LONG")
        self.assertEqual(signals["shock_reversal"][0]["direction"], "SHORT")
        self.assertEqual(signals["momentum_7_30"][0]["direction"], "LONG")

    def test_positive_net_must_survive_removing_best_trade(self):
        result = dict(closed=20, sides={"LONG": {"count": 10}, "SHORT": {"count": 10}},
                      net=1, max_drawdown=.01, unresolved={}, without_best_trade=.1)
        self.assertTrue(study.meets_threshold(result, result))
        self.assertFalse(study.meets_threshold(result, dict(result, without_best_trade=0)))

    def test_half_size_rounds_down_to_exchange_step(self):
        qty, reason = study.core.size_for(3050, 3049, "ETHUSDT", .001, max_notional=50)
        self.assertIsNone(reason)
        self.assertEqual(qty, .016)
        self.assertLessEqual(qty * 3050, 50)
        self.assertEqual(study.core.size_for(3050, 3049, "ETHUSDT", .001), (.032, None))
