import unittest
from research import maker_viability as v
from research.usdm_screen import FRAMES,H

class MakerViabilityTest(unittest.TestCase):
    def test_exact_short_aggregate_notional_reconstruction(self):
        # Two unit shorts: reference entries 100 and 200, exits 98 and 202,
        # with adverse 1% on both sides: 297 entry and 303 exit notional.
        r=v.compute(gross=0,funding=0,impact=6,turnover=600)
        self.assertAlmostEqual(r['entry_notional_saved_fill'],297)
        self.assertAlmostEqual(r['exit_notional_saved_fill'],303)
        self.assertAlmostEqual(r['fee_total'],297*.0002+303*.0005)

    def test_saved_96h_is_not_the_requested_48h(self):
        span,entry_window,exit_window,_=FRAMES['1h']
        self.assertEqual((span,entry_window,exit_window),(H,48,24))
        self.assertEqual(2*entry_window*span,96*H)
        self.assertNotEqual(2*entry_window*span,48*H)

    def test_reconciles_saved_aggregate_and_fixed_list_expectation(self):
        d=v.calculate();b=d['base'];s=d['stress']
        self.assertEqual((b['closed'],s['closed']),(219,229))
        self.assertEqual((b['time_exit_count'],s['time_exit_count']),(5,5))
        self.assertGreater(b['aggregate']['remaining'],0)
        self.assertLess(s['aggregate']['remaining'],0)
        self.assertAlmostEqual(sum(x['remaining'] for x in b['segments'].values()),b['aggregate']['remaining'])
        self.assertAlmostEqual(b['random_fixed_list_expectation']['22']['expected_remaining'],b['aggregate']['remaining']*197/219)

if __name__=='__main__':unittest.main()
