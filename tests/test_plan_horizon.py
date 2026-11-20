from decimal import Decimal as D
import unittest

from research.ai_limit_plan import check_target_horizon, excursion_profile


class HorizonTests(unittest.TestCase):
    def test_disjoint_windows_and_directional_nearest_rank(self):
        rows = [[i, '100','103','99','101','1'] for i in range(32)]
        rows[-1][2] = '150'
        profile = excursion_profile(rows)
        self.assertEqual(profile[0], [300, 100])
        self.assertEqual(profile[15], [5000, 100])

    def test_boundary_floor_and_sides(self):
        snapshot = {'excursion_q75_bps': [[100, 200], [300, 400]]}
        check_target_horizon(snapshot, 'SHORT', D(100), D(98), 29)
        with self.assertRaisesRegex(ValueError, 'outside_hold'):
            check_target_horizon(snapshot, 'LONG', D(100), D('101.01'), 29)
        with self.assertRaisesRegex(ValueError, 'below_candle'):
            check_target_horizon(snapshot, 'SHORT', D(100), D(99), 14)
        check_target_horizon(snapshot, 'LONG', D(100), D(103), 30)
