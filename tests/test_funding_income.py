import unittest
from research import funding_income as study


class FundingIncomeTests(unittest.TestCase):
    def test_only_published_rates_and_receiving_side(self):
        slot = study.SLOT
        t = 3 * slot
        for sign, side in ((1, 'SHORT'), (-1, 'LONG')):
            known = [(i*slot+1, sign*.001) for i in range(1, 4)]
            future = [(4*slot+1, -sign*.02)]
            actual = study.decisions(known+future, t, t+slot)
            self.assertEqual(actual, study.decisions(known, t, t+slot))
            self.assertEqual(actual[0]['direction'], side)
            self.assertTrue(all(ts < actual[0]['signal_ms'] for ts, _ in actual[0]['known_settlements']))

    def test_mixed_missing_and_unpublished_do_not_qualify(self):
        s = study.SLOT
        for bad in ([(s, .002), (2*s, -.001), (3*s, .003)],
                    [(s, .002), (3*s, .002)],
                    [(s, .002), (2*s, .002), (3*s+16*study.core.M, .002)]):
            self.assertEqual(study.decisions(bad, 3*s, 4*s), [])
