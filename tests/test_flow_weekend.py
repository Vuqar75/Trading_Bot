import unittest
import numpy as np
from research import flow_weekend as study
from research import extreme_reversal as core


class Signals(unittest.TestCase):
    def test_weekend_uses_friday_close_and_completed_sunday(self):
        start = core.stamp('2025-01-03') + study.DAY - core.M
        closes = np.full(2882, 100.)
        for value, side in ((101., 'SHORT'), (99., 'LONG')):
            closes[2880] = value
            rows = study.weekend_signals(closes, start, start, start + 4*study.DAY)
            self.assertEqual([(r['signal_ms'], r['direction']) for r in rows],
                             [(core.stamp('2025-01-06'), side)])
            self.assertEqual(rows, study.weekend_signals(closes[:-1], start, start, start + 4*study.DAY))
        closes[50] = np.nan
        self.assertEqual(study.weekend_signals(closes, start, start, start+4*study.DAY), [])

    def test_flow_requires_consistent_nonzero_quintets(self):
        v = np.full((60, 2), 100.)
        v[:, 1] = 60.
        v[15:30, 1] = 40.
        v[30:35] = 0.
        v[50:55, 1] = 40.
        result = study.flow_signals(v, 0, 0, 61 * core.M)
        self.assertEqual([(r['signal_ms'], r['direction']) for r in result],
                         [(15 * core.M, 'LONG'), (30 * core.M, 'SHORT')])
        self.assertEqual(result[:1], study.flow_signals(v[:15], 0, 0, 61 * core.M))
