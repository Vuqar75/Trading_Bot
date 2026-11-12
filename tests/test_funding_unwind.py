import unittest

import numpy as np

from research.funding_unwind import criteria, signals

M = 60000
BAR = 15*M


class FundingUnwindTests(unittest.TestCase):
    def prices(self):
        return np.tile([100., 101., 99., 100.], (60*15, 1))

    def test_threshold_side_and_only_closed_prices(self):
        out = signals(self.prices(), [(30*BAR, .0003), (31*BAR, -.0003), (32*BAR, .00029999)], 0)
        self.assertEqual([r['side'] for r in out], [-1, 1])
        self.assertEqual([r['T'] for r in out], [30*BAR, 31*BAR])
        self.assertTrue(all(r['last_closed_price_ms'] == r['T'] for r in out))
        self.assertTrue(all(not r['settled_funding_is_earned'] for r in out))
        self.assertAlmostEqual(out[0]['atr_pct'], .02)

    def test_future_price_and_future_funding_do_not_change_prior_signal(self):
        prices = self.prices()
        first = signals(prices, [(30*BAR, .0004)], 0)
        prefix = signals(prices[:30*15], [(30*BAR, .0004)], 0)
        self.assertEqual(first, prefix)
        prices[30*15:] *= 5
        self.assertEqual(signals(prices, [(30*BAR, .0004), (45*BAR, -.001)], 0)[0], first[0])

    def test_missing_warmup_not_imputed(self):
        self.assertEqual(signals(self.prices(), [(3*BAR, .001)], 0), [])

    def test_duplicate_invalid_funding_rejected(self):
        for funding in ([(30*BAR, .001), (30*BAR, .001)], [(30*BAR, float('nan'))], [(30*BAR+.1, .001)]):
            with self.assertRaises(ValueError):
                signals(self.prices(), funding, 0)

    def test_actual_millisecond_stamp_rounds_decision_forward(self):
        event = 30*BAR+1
        signal = signals(self.prices(), [(event, .0004)], 0)[0]
        self.assertEqual(signal['funding_event_ms'], event)
        self.assertEqual(signal['last_closed_price_ms'], 30*BAR)
        self.assertEqual(signal['T'], 30*BAR+M)
        self.assertGreater(signal['T']+M, event+M)

    def test_count_pass_does_not_hide_failed_economics(self):
        base = dict(closed=40, directions={-1: 20, 1: 20}, net=1, segments={'a': 1, 'b': 1, 'c': 1, 'd': -1}, without_best=1, drawdown=.01)
        stress = dict(base, net=-1)
        result = criteria(base, stress)
        self.assertTrue(result['counts'])
        self.assertFalse(result['positive_base_stress'])
        self.assertFalse(all(result.values()))


if __name__ == '__main__':
    unittest.main()
