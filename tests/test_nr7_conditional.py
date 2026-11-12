import unittest

import numpy as np

from research import nr7_conditional as r


class ConditionalNR7Tests(unittest.TestCase):
    def order(self):
        return {'time_ms':0,'entry_side':1,'trigger_reference':2000.,'stop_reference':1980.,'activation_ms':r.M,'expiry_ms':15*r.M}

    def test_order_must_exist_before_trigger_and_touch_is_not_enough(self):
        order=self.order();bar=np.array([1999.,2002.,1998.,2001.])
        self.assertIsNone(r.trigger_reference(order,0,bar))
        self.assertEqual(r.trigger_reference(order,r.M,bar),2000.)
        bar[1]=2000.;self.assertIsNone(r.trigger_reference(order,r.M,bar))
        self.assertIsNone(r.trigger_reference(order,15*r.M,np.array([2005.]*4)))

    def test_gap_uses_adverse_open_and_short_mirrors(self):
        order=self.order()
        self.assertEqual(r.trigger_reference(order,r.M,np.array([2010.,2012.,2005.,2011.])),2010.)
        order.update(entry_side=-1,trigger_reference=2000.,stop_reference=2020.)
        self.assertEqual(r.trigger_reference(order,r.M,np.array([1990.,1995.,1985.,1991.])),1990.)

    def test_activation_guard_precedes_any_cash_change(self):
        Account,_=r.account_class();a=Account('ETHUSDT',False,15);a.contexts[0]=self.order()
        a.enter(0,1,2000.,2000.,0)
        self.assertEqual(a.cash,1000.);self.assertIsNone(a.position)
        self.assertEqual(a.events[-1]['reason'],'order_inactive')

    def test_same_minute_entry_stop_is_a_loss_and_funding_credit_unknown(self):
        for rate in (.01,-.01):
            Account,_=r.account_class();a=Account('ETHUSDT',False,15);a.contexts[0]=self.order()
            a.enter(r.M,1,2000.,2000.,0)
            r.entry_minute_funding(a,r.M,np.array([2000.]*4),[(r.M+1,rate)])
            a.tick(r.M,np.array([2000.,2002.,1970.,1990.]),np.array([2000.,2002.,1970.,1990.]),[])
            self.assertEqual(len(a.trades),1);trade=a.trades[0]
            self.assertEqual(trade['reason'],'stop');self.assertLess(trade['net'],0)
            self.assertLessEqual(trade['funding'],0)
            self.assertTrue(trade['funding_events'][0]['entry_time_uncertain'])
            self.assertEqual(trade['entry_window_end'],2*r.M)
            self.assertAlmostEqual(a.cash,1000+trade['net'])

    def test_setup_features_use_no_confirmation_or_future_volatility(self):
        c=100+.1*np.arange(300);bars=np.column_stack((c,c+1,c-1,c));bars[205]=[120.5,120.6,120.4,120.5]
        prices=np.repeat(bars,15,axis=0);full=r.indicators(prices,15);prefix=r.indicators(prices[:206*15],15)
        self.assertEqual(full['side'][205],1)
        for k in full:np.testing.assert_allclose(full[k][:206],prefix[k],equal_nan=True)


if __name__=='__main__':unittest.main()
