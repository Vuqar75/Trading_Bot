from datetime import timedelta
import unittest
from unittest.mock import patch

import numpy as np

from research import funding_seven_day as study


class SevenDayFundingTests(unittest.TestCase):
    def setUp(self):
        self.start = study.core.BEGIN
        self.last = self.start + 2 * study.SLOT
        self.history = [(self.start + i * study.SLOT, 0.00015)
                        for i in range(-study.PAYMENTS + 1, 1)]

    def test_sign_is_based_on_past_signed_rates(self):
        with patch.object(study.core, "END", self.last):
            positive = study.decisions(self.history + [(self.start + study.SLOT, -0.02)])
            negative = study.decisions([(time, -rate) for time, rate in self.history])
        self.assertEqual([row["direction"] for row in positive], ["SHORT", "LONG"])
        self.assertEqual(len(positive[0]["known_settlements"]), study.PAYMENTS)
        self.assertEqual(negative[0]["direction"], "LONG")

    def test_missing_slot_and_offsetting_rates(self):
        mixed = [(time, rate if index % 2 else -rate)
                 for index, (time, rate) in enumerate(self.history)]
        with patch.object(study.core, "END", self.start + study.SLOT):
            self.assertEqual(study.decisions(self.history[:5] + self.history[6:]), [])
            self.assertEqual(study.decisions(mixed), [])

    def test_switching_bound_and_forecast_side(self):
        funding = [(self.start + i * study.SLOT, (-1) ** i * 0.001)
                   for i in range(1, study.PAYMENTS + 1)]
        price = np.full((study.HOLD + 20, 1), 100.)
        signal = self.start + 15 * study.core.M
        end = self.start + timedelta(days=7, minutes=17).total_seconds() * 1000
        with patch.object(study.core, "START", self.start), patch.object(study.core, "END", int(end)):
            (row,) = study.windows(price, price, funding, [{"signal_ms": signal, "direction": "SHORT"}], 1)
        self.assertAlmostEqual(row["best_fixed_side_bps"], 10)
        self.assertAlmostEqual(row["switching_upper_bps"], 210)
        self.assertAlmostEqual(row["forecast_side_bps"], -10)
