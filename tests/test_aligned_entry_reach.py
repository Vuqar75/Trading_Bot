from unittest import TestCase

import pandas as pd

from research import aligned_entry_reach as study


class AlignedEntryReachTests(TestCase):
    def test_short_requires_strict_cross_right_side_and_enough_quantity(self):
        rows = pd.DataFrame(dict(agg_trade_id=[1, 2, 3, 4, 5],
                                 price=[100., 101., 101., 102., 102.],
                                 quantity=[1., 1., .0009, .0019, .001],
                                 is_buyer_maker=[False, True, False, False, False]))
        candidate = dict(direction="SHORT", entry="100", quantity_cap=".002")
        result = study.inspect_candidate(candidate, dict(bid="99", ask="99.1"), rows)
        self.assertEqual(result["status"], "full_trade_through")
        self.assertEqual(result["first_trade_id"], 4)
        self.assertEqual(result["full_trade_id"], 5)

    def test_long_filters_passive_price_and_requires_complete_trade_ids(self):
        rows = pd.DataFrame(dict(agg_trade_id=[10, 11, 13, 14], transact_time=[0, 10, 20, 30]))
        self.assertIsNone(study.archive_window(rows, 0, 30))
        candidate = dict(direction="LONG", entry="99", quantity_cap=".001")
        self.assertEqual(study.inspect_candidate(candidate, dict(bid="98.9", ask="99"), None),
                         {"status": "limit_would_take"})
