import unittest
import numpy as np
from research import donchian_adx as study

class AdxTest(unittest.TestCase):
    def test_warmup_direction_and_causality(self):
        close=np.arange(60.)+100
        candles=np.column_stack((close,close+1,close-1,close))
        prices=np.repeat(candles,study.base.BAR,axis=0)
        result=study.adx(prices)
        self.assertTrue(np.isnan(result[:27]).all())
        np.testing.assert_allclose(result[27:],100)
        np.testing.assert_allclose(study.adx(400-prices[:,[0,2,1,3]]),result)
        shorter=prices[:30*study.base.BAR+100].copy()
        shorter[30*study.base.BAR:]*=2
        np.testing.assert_allclose(study.adx(shorter),result[:30])

    def test_flat_and_reset(self):
        prices=np.full((65*study.base.BAR,4),100.)
        prices[30*study.base.BAR]=np.nan
        result=study.adx(prices)
        self.assertEqual(result[27],0)
        self.assertTrue(np.isnan(result[30:58]).all())
        self.assertEqual(result[58],0)
        self.assertIsNone(study.rejection(25))
        self.assertEqual(study.rejection(np.nextafter(25,0)),'adx_below_25')
        self.assertEqual(study.rejection(np.nan),'adx_unavailable')
