from unittest import TestCase

from research.aligned_ioc_entry import immediate_price


class ImmediateEntryTests(TestCase):
    def test_long_reprices_fixed_bracket_and_blocks_excess_price(self):
        self.assertEqual(immediate_price(1, 60000, 59700, 61200, .0001), 60006)
        self.assertIsNone(immediate_price(1, 60400, 59700, 61200, 0))

    def test_short_reprices_fixed_bracket_and_blocks_adverse_entry(self):
        self.assertEqual(immediate_price(-1, 60000, 60300, 58800, .0001), 59994)
        self.assertIsNone(immediate_price(-1, 59600, 60300, 58800, 0))
        self.assertIsNone(immediate_price(-1, 101000, 101300, 99000, 0))
