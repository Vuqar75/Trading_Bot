import unittest
import numpy as np
from research.action_values import observations,path
from research import extreme_reversal as c

class ActionValuesTest(unittest.TestCase):
    def test_unclosed_bars_cannot_change_features(self):
        p=np.tile([100.,102.,98.,100.],(2200,1));v=np.ones(len(p))
        ref=observations(p,v).iloc[1]
        t=int(ref['at'])
        p[t:]*=1.17;v[t:]*=4
        self.assertTrue(ref.equals(observations(p,v).iloc[1]))
    def test_ambiguous_stop_both_sides_and_gap(self):
        p=np.tile([100.,100.,100.,100.],(300,1))
        ft=np.array([],dtype=int);fr=np.array([])
        p[2]=[100.,104.,96.,100.]
        for side in (1,-1):
            r=path(0,1,side,False,p,p,ft,fr,c.START)
            self.assertTrue(r['ambiguous'])
            self.assertEqual(r['why'],'SL')
            self.assertLess(r['net'],0)
        p[2]=[95.,96.,94.,95.]
        r=path(0,1,1,False,p,p,ft,fr,c.START)
        self.assertAlmostEqual(r['exit'],95*.9995)
    def test_short_funding_and_time(self):
        p=np.tile([100.,100.,100.,100.],(300,1))
        r=path(0,2,-1,False,p,p,np.array([c.START+50*c.M]),np.array([.001]),c.START)
        self.assertEqual((r['why'],r['j']-r['i']),('TIME',240))
        self.assertAlmostEqual(r['funding'],.1)
        self.assertAlmostEqual(r['net'],r['gross']-r['impact']-r['fees']+.1)
