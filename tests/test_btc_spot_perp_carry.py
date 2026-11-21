from unittest import TestCase
from unittest.mock import patch

import numpy as np

from research import btc_spot_perp_carry as carry


class SpotPerpCarryTests(TestCase):
    def test_signal_requires_positive_funding_and_uses_closed_price(self):
        start = carry.core.BEGIN
        slot = start // carry.SLOT
        spot = np.full((17, 4), 100.)
        perp = np.full_like(spot, 100.6)
        perp[15:, 3] = 100.
        funding = [(start + n * carry.SLOT, .0001)
                   for n in range(-carry.PAYMENTS + 1, 2)]
        with patch.object(carry.core, "START", start):
            signal = carry.signal_at(slot, spot, perp, funding, [t for t, _ in funding])
            self.assertTrue(signal["entry_signal"])
            self.assertAlmostEqual(signal["score_bp"], 81)
            negative = [(t, -rate) for t, rate in funding]
            self.assertFalse(carry.signal_at(slot, spot, perp, negative,
                                             [t for t, _ in negative])["entry_signal"])

    def test_price_move_cancels_and_costs_apply_to_both_legs(self):
        start = carry.core.BEGIN
        spot = np.full((carry.HOLD + 2, 4), 100.)
        spot[-1] = 101.
        perp = spot.copy()
        funding = [(start + n * carry.SLOT, .0001) for n in range(1, carry.PAYMENTS + 1)]
        with patch.object(carry.core, "START", start):
            row = carry.window({"signal_ms": start}, spot, perp, perp, funding,
                               [t for t, _ in funding], 1, .0005)
        self.assertEqual(row["gross"], 0)
        self.assertAlmostEqual(row["funding"], .21)
        self.assertAlmostEqual(row["impact"], .201)
        self.assertAlmostEqual(row["fees"], .30149975)
        self.assertAlmostEqual(row["net"], -.29249975)
