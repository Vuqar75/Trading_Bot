import unittest
import numpy as np
from research import panel_net_ml as p


class PanelNetTests(unittest.TestCase):
    def market(self,n=60*240):
        close=8+np.sin(np.arange(n)/50)*.02
        trade=np.column_stack((close,close+.04,close-.04,close))
        fund=[(t,.0001 if t%(16*p.H)==0 else -.0001) for t in range(0,n*p.M,8*p.H)]
        return trade,trade.copy(),np.ones(n),fund

    def test_crossasset_features_use_only_closed_prefix_and_lagged_funding(self):
        market=self.market();markets={s:market for s in p.ASSETS}
        full=p.features(markets,0)
        cut=60*190
        prefix=p.features({s:(a[:cut],b[:cut],v[:cut],f) for s,(a,b,v,f) in markets.items()},0)
        for s in p.ASSETS:
            np.testing.assert_allclose(full[s]['X'][:190],prefix[s]['X'],equal_nan=True)
            self.assertEqual(full[s]['X'].shape[1],len(p.FEATURES))
            mask=np.isfinite(full[s]['X']).all(1)
            self.assertTrue(np.all(full[s]['funding_source_ms'][mask]<=full[s]['times'][mask]-15*p.M))

    def test_boundary_labels_are_purged_not_just_signal_time(self):
        a=p.core.stamp('2025-07-01');b=p.core.stamp('2025-08-01')
        rows=[{'T':a-p.H,'label_end_ms':a},{'T':a-2*p.H,'label_end_ms':a-1},
              {'T':a,'label_end_ms':a+4*p.H},{'T':b,'label_end_ms':b+4*p.H}]
        train,test,_=p.split(rows,a,b)
        self.assertEqual(train,[rows[1]]);self.assertEqual(test,[rows[2]])

    def test_vectorized_labels_match_financial_handler_for_both_sides_and_stops(self):
        for side in (-1,1):
            for stop in (False,True):
                market=list(self.market(n=60*20));t=8*p.H
                if stop:
                    i=(t+6*p.M)//p.M+17
                    market[0]=market[0].copy();market[1]=market[1].copy()
                    market[0][i,2 if side==1 else 1]=7.7 if side==1 else 8.3
                    market[1][i]=market[0][i]
                market=tuple(market);expected=p.label(t,side,'ADAUSDT',market,0)
                Account,_=p.account_class();a=Account('ADAUSDT',True)
                a.contexts[t]={'predicted_net':.001,'model':'HGBR'};a.signal(t,side,{})
                first=t//p.M;last=first+247
                for i in range(first,last):
                    events=[e for e in market[3] if i*p.M<=e[0]<(i+1)*p.M]
                    a.tick(i*p.M,market[0][i],market[1][i],events)
                    if a.trades:break
                self.assertEqual(len(a.trades),1)
                actual=a.trades[0]
                for field in ('net','gross','fees','impact','funding','qty','entry','exit','entry_ms','exit_ms'):
                    self.assertAlmostEqual(actual[field],expected[field],places=9,msg=field)
                self.assertEqual(actual['reason'],expected['reason'])

    def test_threshold_global_ranking_has_no_actual_labels(self):
        rows=[{'model':'HGBR','symbol':s,'T':0,'fold':'2025-07','feature_sha256':'x',
               'long':.001 if s in ('ADAUSDT','XRPUSDT') else .00049,'short':.001 if s=='ADAUSDT' else -.01} for s in p.ASSETS]
        choices=p.candidates(rows,'HGBR')[0]
        self.assertEqual([(r['symbol'],r['entry_side']) for r in choices],[('ADAUSDT',1),('ADAUSDT',-1),('XRPUSDT',1)])
        self.assertEqual(p.candidates(rows,'Ridge'),{})


if __name__=='__main__':unittest.main()
