import unittest

import numpy as np

from research import range_patterns as r


class RangePatternTests(unittest.TestCase):
    def bars(self):
        c=100+.1*np.arange(300)
        return np.column_stack((c,c+1,c-1,c))

    def test_nr7_only_next_closed_confirmation_and_known_stop(self):
        bars=self.bars();i=205
        bars[i]=[120.5,120.6,120.4,120.5]
        bars[i+1]=[120.6,121.2,120.,121.1]
        data=r.indicators(np.repeat(bars,15,axis=0),15)
        self.assertEqual(data['nr7_side'][i+1],1)
        self.assertEqual(data['nr7_trigger'][i+1],120.6)
        self.assertEqual(data['nr7_stop'][i+1],120.4)
        bars[i+1,3]=120.;bars[i+2]=[120.,122.,119.9,121.5]
        data=r.indicators(np.repeat(bars,15,axis=0),15)
        self.assertEqual(data['nr7_side'][i+1],0)
        self.assertEqual(data['nr7_side'][i+2],0)

    def test_failed_break_excludes_current_and_uses_last_extreme_age(self):
        bars=self.bars();bars[240,1]=135.;bars[250]=[125.,136.,124.,130.]
        data=r.indicators(np.repeat(bars,15,axis=0),15)
        self.assertEqual(data['soup_side'][250],-1)
        self.assertEqual(data['soup_trigger'][250],135.)
        self.assertEqual(data['soup_stop'][250],136.)
        self.assertEqual(data['age_high'][250],10)
        bars[249,1]=135.
        data=r.indicators(np.repeat(bars,15,axis=0),15)
        self.assertEqual(data['age_high'][250],1)
        self.assertEqual(data['soup_side'][250],0)

    def test_double_sweep_cancels(self):
        bars=self.bars();bars[240,1]=135.;bars[250]=[125.,136.,120.,130.]
        data=r.indicators(np.repeat(bars,15,axis=0),15)
        self.assertEqual(data['soup_side'][250],0)

    def test_prefix_invariance(self):
        prices=np.repeat(self.bars(),60,axis=0)
        full=r.indicators(prices,60);prefix=r.indicators(prices[:250*60],60)
        for k in full:np.testing.assert_allclose(full[k][:250],prefix[k],equal_nan=True)

    def test_delayed_trigger_and_stop_checked_before_financial_change(self):
        Account,_=r.account_class();a=Account('ETHUSDT',False,15,'nr7')
        a.contexts[0]={'trigger_reference':1990.,'stop_reference':1980.}
        a.enter(60000,1,1989.,1989.,0)
        self.assertIsNone(a.position);self.assertEqual(a.cash,1000.)
        a.contexts[0]['stop_reference']=1900.;a.enter(60000,1,2000.,2000.,0)
        self.assertIsNone(a.position);self.assertEqual(a.cash,1000.)
        a.contexts[0]['stop_reference']=1980.;a.enter(60000,1,2000.,2000.,0)
        self.assertEqual(a.position['stop'],1980.)
        self.assertLess(a.position['stop_pct'],.02)
        self.assertLessEqual(a.position['estimated_risk'],5)

    def test_trend_exit_label_is_separate_from_mean_exit(self):
        for family,reason in (('nr7','trend_exit'),('soup','mean_exit')):
            Account,_=r.account_class();a=Account('ETHUSDT',False,15,family)
            a.contexts[0]={'trigger_reference':1990.,'stop_reference':1980.};a.enter(60000,1,2000.,2000.,0)
            a.signal(120000,0,{'time_ms':120000,'close':2000.,'exit_line':2001.})
            a.tick(180000,np.array([2000.]*4),np.array([2000.]*4),[])
            self.assertEqual(len(a.trades),1);self.assertEqual(a.trades[0]['reason'],reason)


if __name__=='__main__':unittest.main()
