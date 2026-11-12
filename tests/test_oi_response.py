import unittest

import numpy as np

from research import oi_response as r


class OIResponseTests(unittest.TestCase):
    def fixture(self,drop=True):
        bars=np.tile([100.,100.1,99.9,100.],(200,1));bars[100]=[100.,100.,95.5,96.];bars[101]=[96.,97.1,95.6,96.5]
        ts=np.arange(0,200*r.SPAN+1,5*r.M,dtype=np.int64);oi=np.full(len(ts),1000.)
        if drop:oi[ts>=101*r.SPAN]=980.
        return np.repeat(bars,15,axis=0),ts,oi

    def test_lagged_oi_and_pre_shock_anchor(self):
        prices,ts,oi=self.fixture();d=r.indicators(prices,ts,oi,0);i=101;t=(i+1)*r.SPAN
        self.assertEqual(d['oi_filter'][i],1)
        self.assertEqual(d['oi_end_ms'][i],t-r.SPAN)
        self.assertLessEqual(d['oi_end_ms'][i],t-15*r.M)
        self.assertEqual(d['anchor'][i],100.)
        self.assertEqual(d['anchor_asof_ms'][i],100*r.SPAN)
        self.assertAlmostEqual(d['oi_drop'][i],-.02)

    def test_price_control_does_not_fake_oi_from_price_decline(self):
        prices,ts,oi=self.fixture(False);d=r.indicators(prices,ts,oi,0)
        self.assertEqual(d['price_control'][101],1)
        self.assertEqual(d['oi_filter'][101],0)
        self.assertEqual(d['oi_drop'][101],0)

    def test_missing_metrics_block_both_arms_not_zero_fill(self):
        prices,ts,oi=self.fixture();end=101*r.SPAN
        keep=(ts<end-10*r.M)|(ts>end)
        d=r.indicators(prices,ts[keep],oi[keep],0)
        self.assertFalse(d['metric_valid'][101])
        self.assertEqual(d['price_control'][101],0);self.assertEqual(d['oi_filter'][101],0)
        self.assertTrue(np.isnan(d['oi_drop'][101]))

    def test_prices_and_metrics_after_decision_do_not_change_it(self):
        prices,ts,oi=self.fixture();full=r.indicators(prices,ts,oi,0)
        prefix=r.indicators(prices[:102*15],ts,oi,0)
        for k in full:np.testing.assert_allclose(full[k][:102],prefix[k],equal_nan=True)
        oi[ts>101*r.SPAN]=100.
        changed=r.indicators(prices,ts,oi,0)
        self.assertEqual(full['oi_drop'][101],changed['oi_drop'][101])

    def test_delayed_geometry_before_fees_and_known_stop(self):
        Account,_=r.account_class();a=Account('ETHUSDT',False)
        a.contexts[0]={'exit_anchor':2060.,'stop_reference':1980.,'oi_end_ms':-r.SPAN,'oi_start_ms':-r.SPAN-60*r.M,'variant':'oi_filter'}
        a.enter(r.M,1,2055.,2055.,0)
        self.assertIsNone(a.position);self.assertEqual(a.cash,1000.)
        a.enter(r.M,1,2000.,2000.,0)
        self.assertEqual(a.position['exit_anchor'],2060.)
        self.assertEqual(a.position['stop'],1980.)
        self.assertLessEqual(a.position['estimated_risk'],5)


if __name__=='__main__':unittest.main()
