import unittest

import numpy as np

from research.btc_eth_delay import HOUR, signals_from


def minutes(closes):
    return np.repeat(np.asarray(closes, dtype=float), 60 * 4).reshape(-1, 4)


def compare(btc, eth):
    return signals_from(btc, eth, 0, 0, (len(btc) // 60 + 1) * HOUR)


class TestDelayedResponse(unittest.TestCase):
    def test_sign_and_causal_boundary(self):
        btc = minutes([100, 102, 99])
        eth = minutes([100, 100.4, 100.1])
        signals, valid, _ = compare(btc, eth)
        self.assertEqual([(s["signal_ms"], s["direction"]) for s in signals],
                         [(2 * HOUR, "LONG"), (3 * HOUR, "SHORT")])
        self.assertEqual(valid, 2)
        self.assertEqual(compare(btc[:120], eth[:120])[0], signals[:1])

    def test_opposite_excessive_and_small_responses(self):
        for lead, follow in [(102, 99), (102, 101.5), (100.5, 100.1)]:
            with self.subTest(lead=lead, follow=follow):
                self.assertEqual(compare(minutes([100, lead]), minutes([100, follow]))[0], [])

    def test_gap_invalidates_both_returns_using_that_hour(self):
        btc = minutes([100, 103, 106])
        eth = minutes([100, 100.1, 100.2])
        eth[75, 0] = np.nan
        self.assertEqual(compare(btc, eth), ([], 0, 2))
