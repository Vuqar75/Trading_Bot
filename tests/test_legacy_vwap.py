import unittest

import numpy as np

from research import legacy_vwap as p


class LegacyPortfolioTests(unittest.TestCase):
    def test_coarse_qty_cap_and_modelled_minimum(self):
        for symbol in p.ASSETS:
            for price in (.2,1.,3.,8.,12.,30.):
                q,error=p.sizing(price,price,symbol,.001)
                self.assertIsNone(error);self.assertLessEqual(q*price,100+1e-8);self.assertGreaterEqual(q*price,50)
                self.assertEqual(q% (10 if price<=10 else 1),0)
        self.assertEqual(p.sizing(101.,101.,'LINKUSDT',.001)[1],'strategy_granularity_or_cap')

    def test_selection_is_current_room_then_symbol_not_future_return(self):
        data={s:{'side':np.array([0]),'close':np.array([1.]),'vwap':np.array([1.])} for s in p.ASSETS}
        for s in ('ADAUSDT','XRPUSDT'):data[s].update(side=np.array([1]),vwap=np.array([1.02]))
        chosen=p.choose(data,0)
        self.assertEqual(chosen[0]['symbol'],'ADAUSDT')
        data['XRPUSDT']['vwap'][0]=1.03
        self.assertEqual(p.choose(data,0)[0]['symbol'],'XRPUSDT')

    def test_single_account_two_assets_preserves_cash_and_symbol_funding(self):
        Account,_=p.account_class();a=Account('ADAUSDT',False,60)
        a.contexts[0]={'vwap':1.05};a.enter(60000,1,1.,1.,0)
        self.assertEqual(a.position['symbol'],'ADAUSDT')
        a.tick(120000,np.array([1.01,1.02,1.0,1.01]),np.array([1.01,1.02,1.0,1.01]),[(120001,.0001)])
        a.close(180000,1.02,'time')
        first_cash=a.cash;a.symbol='DOTUSDT';a.contexts[180000]={'vwap':7.5}
        a.enter(240000,-1,8.,8.,180000)
        self.assertEqual(a.position['symbol'],'DOTUSDT')
        a.close(300000,7.9,'time')
        self.assertEqual(len(a.trades),2)
        self.assertEqual({x['symbol'] for x in a.trades},{'ADAUSDT','DOTUSDT'})
        self.assertAlmostEqual(a.cash,1000+sum(x['net'] for x in a.trades))
        self.assertLess(a.trades[0]['funding'],0);self.assertEqual(a.trades[1]['funding'],0)
        self.assertGreater(first_cash,999.)


if __name__=='__main__':unittest.main()
