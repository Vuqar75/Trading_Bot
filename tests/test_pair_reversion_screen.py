import unittest

import numpy as np

from research.pair_reversion_screen import M, evaluate, signals, summarize


def market(prices):
    return np.repeat(np.asarray(prices, dtype=float)[:, None], 4, axis=1)


class PairScreenTests(unittest.TestCase):
    def test_prior_only_crossing_and_direction(self):
        spread = .001*np.sin(np.arange(150))
        spread[120] = .03
        btc = market(np.full(150*15, 100.))
        eth = market(np.repeat(100*np.exp(spread), 15))
        out = signals(btc, eth, 0)
        first = out[0]
        self.assertEqual(first['signal_ms'], 121*15*M)
        self.assertEqual(first['eth_side'], -1)
        self.assertAlmostEqual(first['prior_mean'], spread[24:120].mean())
        self.assertAlmostEqual(first['prior_std'], spread[24:120].std(ddof=1))
        self.assertEqual(signals(btc[:121*15], eth[:121*15], 0), out[:1])
        # Future corruption cannot change an already formed signal.
        eth[121*15:] *= 5
        self.assertEqual(signals(btc, eth, 0)[0], first)

    def test_reverse_spread_reverses_legs(self):
        spread = .001*np.sin(np.arange(150))
        spread[120] = -.03
        out = signals(market(np.ones(2250)*100), market(np.repeat(100*np.exp(spread), 15)), 0)
        self.assertEqual(out[0]['eth_side'], 1)

    def test_both_leg_costs_and_funding_boundaries(self):
        btc = market(np.full(260, 100.))
        eth = market(np.full(260, 200.))
        eth[241:] = 210
        funding = [(M, .9), (2*M, .001), (241*M, .9)]
        markets = {'BTCUSDT': (btc, btc, funding), 'ETHUSDT': (eth, eth, funding)}
        row = evaluate({'signal_ms': 0, 'eth_side': 1}, markets, False, 0)
        self.assertEqual(row['entry_ms'], M)
        self.assertEqual(row['exit_ms'], 241*M)
        self.assertAlmostEqual(row['gross'], .05)
        self.assertAlmostEqual(row['funding'], 0)
        for leg in row['legs']:
            self.assertEqual(len(leg['funding_events']), 1)
            side, q = leg['side'], leg['qty']
            direct = side*q*(leg['exit_fill_model']-leg['entry_fill_model'])-leg['fees']+leg['funding']
            self.assertAlmostEqual(leg['net'], direct)
            self.assertGreater(leg['fees'], 0)
            self.assertGreater(leg['impact'], 0)
        self.assertAlmostEqual(row['net'], row['gross']-row['fees']-row['impact']+row['funding'])

    def test_stress_timing_and_incomplete_window(self):
        prices = market(np.ones(247)*100)
        markets = {s: (prices, prices, []) for s in ('BTCUSDT', 'ETHUSDT')}
        stress = evaluate({'signal_ms': 0, 'eth_side': -1}, markets, True, 0)
        self.assertEqual(stress['entry_ms'], 6*M)
        self.assertEqual(stress['exit_ms'], 246*M)
        self.assertAlmostEqual(stress['net'], -.006)
        self.assertIsNone(evaluate({'signal_ms': M, 'eth_side': 1}, markets, True, 0))
        prices[100, 0] = np.nan
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            evaluate({'signal_ms': 0, 'eth_side': 1}, markets, True, 0)

    def test_empty_is_not_zero_profit(self):
        result = summarize([])
        self.assertIsNone(result['mean_net_bps'])
        self.assertIsNone(result['worst_adverse_mark_bound_bps'])


if __name__ == '__main__':
    unittest.main()
