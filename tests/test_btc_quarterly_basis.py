from unittest import TestCase

from research import btc_quarterly_basis as study


class QuarterlyBasisTests(TestCase):
    def test_net_equals_change_in_basis_minus_both_legs_costs(self):
        row = study.economics(100., 102., 120., 120.5, .001)
        self.assertAlmostEqual(row["gross"], 1.5)
        self.assertAlmostEqual(row["impact"], .4425)
        self.assertAlmostEqual(row["fees"], .33123925)
        self.assertAlmostEqual(row["net"], .72626075)

    def test_quantity_uses_both_leg_prices_and_free_capital(self):
        self.assertEqual(study.size(90_000, 92_000, 93_000, 1000), (.001, None))
        self.assertEqual(study.size(90_000, 92_000, 101_000, 1000), (0, "minimum_or_cap"))
        self.assertEqual(study.size(90_000, 92_000, 93_000, 180), (0, "capital"))
