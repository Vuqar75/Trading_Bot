import unittest
from research import maker_economics as m

class MakerEconomicsTest(unittest.TestCase):
    def test_two_sided_turnover_denominator(self):
        r=m.measures(gross=1.,funding=-.2,turnover=2000.)
        self.assertAlmostEqual(r['breakeven_bps'],8.)
        self.assertAlmostEqual(r['maker_maker_fee'],.4)
        self.assertAlmostEqual(r['maker_taker_fee_approx'],.7)
        self.assertAlmostEqual(r['maker_taker_headroom_bps_approx'],1.)

    def test_negative_funding_cannot_be_ignored(self):
        r=m.measures(.4,-.8,2000.)
        self.assertLess(r['breakeven_bps'],0)
        with self.assertRaises(ValueError):m.measures(1,0,0)

    def test_saved_sources_reconcile_and_include_mtm_flag(self):
        rows=m.calculate()
        self.assertTrue(rows)
        self.assertTrue(any(x['marked_open_segments'] for x in rows))
        self.assertTrue(all(x['gross']>0 and x['turnover_both_sides']>0 for x in rows))
        self.assertTrue(any(x['study']=='session_momentum' for x in rows))

if __name__=='__main__':unittest.main()
