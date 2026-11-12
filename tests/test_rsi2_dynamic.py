import json
import unittest

import numpy as np

from research import rsi2_dynamic as r


class RSI2DynamicTests(unittest.TestCase):
    def prices(self, minutes=60):
        close = 100+.05*np.arange(300)
        close[250] = close[249]-.5
        close[251] = close[250]-.5
        close *= 20
        bars = np.column_stack((close, close*1.0001, close*.9999, close))
        return np.repeat(bars, minutes, axis=0)

    def test_indicators_causal_and_flat_is_neutral(self):
        for minutes in r.HOLDS:
            prices = self.prices(minutes)
            full = r.indicators(prices, minutes)
            short = r.indicators(prices[:260*minutes], minutes)
            for k in full:
                np.testing.assert_allclose(full[k][:260], short[k], equal_nan=True)
            self.assertEqual(full['side'][251], 1)
            flat = r.indicators(np.ones_like(prices)*100, minutes)
            self.assertTrue(np.all(flat['rsi'][200:] == 50))
            self.assertFalse(flat['side'].any())

    def test_no_entry_before_200_real_closed_bars(self):
        data = r.indicators(self.prices(), 60)
        self.assertFalse(data['side'][:199].any())
        self.assertTrue(np.isnan(data['sma200'][:199]).all())

    def test_dynamic_exit_is_scheduled_from_closed_mean(self):
        prices = self.prices()
        data = r.indicators(prices, 60)
        for stress in (False, True):
            summary, journal = r.simulate(prices, prices, [], data, 'ETHUSDT', 60, stress,
                                         r.core.START+240*60*r.M, r.core.START+300*60*r.M)
            self.assertEqual(summary['closed'], 1)
            trade = journal['trades'][0]
            self.assertEqual(trade['reason'], 'mean_exit')
            decision = next(d for d in journal['decisions'] if d['action']=='mean_exit')
            self.assertGreater(decision['close'], decision['sma5'])
            self.assertEqual(trade['exit_ms'], decision['time_ms']+(6 if stress else 1)*r.M)
            self.assertAlmostEqual(summary['cash'], 1000+summary['net'])
            self.assertIsNone(summary['open']); self.assertIsNone(summary['pending'])
            json.dumps(summary, allow_nan=False)

    def test_time_deadline_preempts_pending_without_second_close(self):
        original = r.core.size_for
        Account, _ = r.account_class()
        self.assertIs(r.core.size_for, original)
        a = Account('ETHUSDT', False, 15)
        a.enter(0, 1, 2000., 2000., 0)
        deadline = a.position['deadline_ms']
        a.pending = {'due_ms': deadline+r.M, 'target': 0, 'signal_ms': deadline-r.M}
        a.tick(deadline, np.array([2000.]*4), np.array([2000.]*4), [])
        self.assertIsNone(a.pending); self.assertIsNone(a.position)
        self.assertEqual(len(a.trades), 1)
        self.assertEqual(a.trades[0]['reason'], 'time')

    def test_two_percent_gap_stop_remains_enabled(self):
        Account, _ = r.account_class(); a = Account('ETHUSDT', False, 60)
        a.enter(0, 1, 2000., 2000., 0)
        a.tick(r.M, np.array([1900., 1920., 1850., 1910.]), np.array([1900., 1920., 1850., 1910.]), [])
        self.assertEqual(a.trades[0]['reason'], 'stop')
        self.assertEqual(a.trades[0]['exit_reference'], 1900.)
        self.assertLess(a.trades[0]['net'], 0)
        self.assertLessEqual(a.trades[0]['estimated_risk'], 5)


if __name__ == '__main__':
    unittest.main()
