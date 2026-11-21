import unittest
import numpy as np
from research.trend_exit import deadline, reversals
from research.action_values import path
from research.extreme_reversal import START

class TrendExitTest(unittest.TestCase):
    def test_reversal_uses_complete_hour_and_only_while_open(self):
        prices=np.tile([100.,101.,99.,100.],(360,1))
        prices[299]=[100.,105.,99.,104.]
        self.assertEqual(reversals(prices[:299])[-1].size,0)
        signals=reversals(prices)
        self.assertEqual(deadline(280,-1,6,signals),(306,'REV'))
        self.assertEqual(deadline(299,-1,1,signals),(3180,'CAP'))
    def test_protection_can_override_reversal_open(self):
        prices=np.tile([100.,100.,100.,100.],(300,1))
        rates=np.array([])
        args=(0,2,-1,False,prices,prices,rates,rates,START)
        self.assertEqual(path(*args,scheduled=(55,'REV'))['why'],'REV')
        prices[55]=[110.,110.,110.,110.]
        result=path(*args,scheduled=(55,'REV'))
        self.assertEqual(result['why'],'SL')
        self.assertAlmostEqual(result['exit'],110*1.0005)
    def test_legacy_time_still_takes_deadline_open(self):
        prices=np.tile([100.,100.,100.,100.],(300,1))
        prices[241]=[110.,110.,110.,110.]
        rates=np.array([])
        self.assertEqual(path(0,2,-1,False,prices,prices,rates,rates,START)['why'],'TIME')
