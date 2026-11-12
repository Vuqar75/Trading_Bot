import unittest

import numpy as np

from research import vwap_dynamic as v


class VWAPDynamicTests(unittest.TestCase):
    def fixture(self, short=False):
        n=96*3;bars=np.tile([100.,100.05,99.95,100.],(n,1));volume=np.ones(n)
        i=2*96+47
        bars[i-1]=[100.,100.,94.5,95.];volume[i-1]=6
        bars[i]=[95.5,98.5,94.5,98.]
        if short:
            o,h,l,c=bars.T.copy();bars=np.column_stack((200-o,200-l,200-h,200-c))
        return np.repeat(bars,15,axis=0),np.repeat(volume/15,15),i

    def test_weighted_moments_and_daily_reset(self):
        prices,volume,i=self.fixture()
        data=v.indicators(prices,volume,15,0)
        bars=prices.reshape(-1,15,4);tp=(bars[:,:,1].max(1)+bars[:,:,2].min(1)+bars[:,-1,3])/3
        w=volume.reshape(-1,15).sum(1);ix=slice(2*96,i+1)
        mean=np.average(tp[ix],weights=w[ix]);variance=np.average((tp[ix]-mean)**2,weights=w[ix])
        self.assertAlmostEqual(data['vwap'][i],mean)
        self.assertAlmostEqual(data['sigma'][i]**2,variance,places=9)
        self.assertAlmostEqual(data['vwap'][2*96],tp[2*96])

    def test_long_short_reentry_and_units(self):
        for short in (False,True):
            prices,volume,i=self.fixture(short);data=v.indicators(prices,volume,15,0)
            self.assertEqual(data['side'][i],-1 if short else 1)
            self.assertGreaterEqual(abs(data['vwap'][i]/data['close'][i]-1),.006)
            self.assertTrue(data['rsi14'][i]>60 if short else data['rsi14'][i]<40)

    def test_prefix_causality_and_missing_volume(self):
        prices,volume,i=self.fixture();full=v.indicators(prices,volume,15,0)
        short=v.indicators(prices[:(i+1)*15],volume[:(i+1)*15],15,0)
        for name in full:np.testing.assert_allclose(full[name][:i+1],short[name],equal_nan=True)
        volume[0]=np.nan
        with self.assertRaises(ValueError):v.indicators(prices,volume,15,0)

    def test_gap_room_checked_before_fees_or_position(self):
        Account,_=v.account_class();a=Account('ETHUSDT',False,60)
        a.contexts[0]={'vwap':2002.};a.enter(60000,1,2000.,2000.,0)
        self.assertIsNone(a.position);self.assertEqual(a.cash,1000.)
        self.assertEqual(a.events[-1]['reason'],'vwap_distance_after_delay')
        a.contexts[0]={'vwap':2020.};a.enter(60000,1,2000.,2000.,0)
        self.assertIsNotNone(a.position)
        self.assertEqual(a.position['deadline_ms'],60000+360*v.M)
        self.assertAlmostEqual(a.position['stop'],a.position['entry']*.98)


if __name__=='__main__':unittest.main()
