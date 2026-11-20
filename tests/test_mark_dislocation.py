import unittest

import numpy as np

from research import mark_dislocation as study
from research import extreme_reversal as engine


class MarkDislocationTests(unittest.TestCase):
    def test_only_new_crossings_on_completed_pairs(self):
        trade = np.array([10000, 9960, 9950, 10040, np.nan, 9900, 9900.])
        mark = np.full(len(trade), 10000.)
        rows, valid, missing = study.crossings(trade, mark, 0, 0, 10 * engine.M)
        self.assertEqual([(r["signal_ms"] // engine.M, r["direction"]) for r in rows],
                         [(2, "LONG"), (4, "SHORT")])
        self.assertEqual((valid, missing), (4, 2))
        self.assertEqual(study.crossings(trade[:4], mark[:4], 0, 0, 10 * engine.M)[0], rows)

    def test_pending_and_exit_boundary_no_duplicate_and_fills_use_trade(self):
        trade = np.full((60, 4), 100.)
        mark = np.full((60, 4), 101.)
        def signal(i):
            return dict(signal_ms=engine.START + i * engine.M, direction="LONG", gap_bps=-45.)
        records, account = study.simulate([signal(i) for i in (1, 3, 22, 23)], trade, mark, {}, "ETHUSDT", True)
        self.assertEqual([i for i, r in enumerate(records) if "net" in r], [0, 3])
        self.assertEqual(records[0]["entry_reference"], 100.)
        self.assertEqual(records[0]["exit_reference"], 100.)
        self.assertAlmostEqual(records[0]["remaining_gap_bps"], 1e4 / 101)
        self.assertLess(records[0]["net"], 0)
        self.assertAlmostEqual(account["cash"], 1000 + records[0]["net"] + records[3]["net"])

    def test_rejected_entry_releases_only_after_attempt(self):
        trade = np.full((60, 4), 100.)
        mark = trade.copy()
        mark[7] = 110.
        signals = [dict(signal_ms=engine.START + i * engine.M, direction="SHORT", gap_bps=41.)
                   for i in (1, 4, 7, 8)]
        rows, _ = study.simulate(signals, trade, mark, {}, "ETHUSDT", True)
        self.assertEqual(rows[0]["rejection"], "price_mark_bound")
        self.assertTrue(all("ignored" in r for r in rows[1:3]))
        self.assertEqual(rows[3]["direction"], "SHORT")
        self.assertLess(rows[3]["net"], 0)
