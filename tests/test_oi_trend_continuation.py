from unittest import TestCase
from unittest.mock import patch

import numpy as np

from research import oi_trend_continuation as study


class OITrendContinuationTests(TestCase):
    def test_uses_natural_oi_before_signal_and_handles_both_directions(self):
        start = study.BEGIN - 8 * 60 * study.core.M
        trade = np.full((721, 4), 100.)
        trade[479, 3] = 101.
        trade[719, 3] = 99.
        trade[720, 3] = 120.
        expected = study.BEGIN + np.array([-255, -15, 225, 240]) * study.core.M
        with patch.object(study.core, "START", start), patch.object(
            study.core, "END", study.BEGIN + 2 * study.BAR
        ):
            rows, availability = study.signals(trade, expected, np.array([100, 101, 100, 200]))
        self.assertEqual(availability["metric_missing"], 0)
        self.assertEqual([(row["direction"], row["oi_pass"]) for row in rows],
                         [("LONG", True), ("SHORT", False)])
        self.assertEqual(rows[1]["oi_end_ms"], expected[2])

    def test_rejects_stale_oi_for_both_policies(self):
        start = study.BEGIN - 8 * 60 * study.core.M
        trade = np.full((481, 4), 100.)
        trade[479, 3] = 90.
        expected = study.BEGIN + np.array([-255, -21]) * study.core.M
        with patch.object(study.core, "START", start), patch.object(
            study.core, "END", study.BEGIN + study.BAR
        ):
            rows, availability = study.signals(trade, expected, np.array([100, 200]))
        self.assertEqual(rows, [])
        self.assertEqual(availability["metric_missing"], 1)
