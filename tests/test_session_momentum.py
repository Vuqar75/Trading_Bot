import datetime as dt
import hashlib
import pathlib
import tempfile
import unittest
import zipfile
import numpy as np
from research import session_momentum as s

class SessionMomentumTest(unittest.TestCase):
    def test_fixed_est_boundary_and_previous_day(self):
        t=s.boundaries(dt.date(2025,7,1))
        self.assertEqual(dt.datetime.fromtimestamp(t['signal']/1000,dt.timezone.utc).hour,21)
        self.assertEqual(t['close']-t['previous'],24*60*s.MINUTE)

    def test_signs_and_strict_zero(self):
        self.assertEqual(s.direction(.01,-.02),'LONG')
        self.assertEqual(s.direction(0,0),'SHORT')
        self.assertIsNone(s.direction(.01,.01))

    def test_future_target_missing_does_not_change_signal(self):
        a=np.full((2000,2),100.); base=s.START+1000*s.MINUTE
        times={'previous':base-600*s.MINUTE,'first':base-500*s.MINUTE,'prelast':base-60*s.MINUTE,'signal':base,'close':base+30*s.MINUTE}
        a[s.idx(times['close'])-1]=np.nan
        value=s.feature(a,times)
        self.assertIsNotNone(value);self.assertIsNone(value[2])
        self.assertEqual(s.direction(*value[:2]),'SHORT')

    def test_minimum_rounds_up_and_net_reconciles(self):
        self.assertGreaterEqual(s.quantity(76123,50)*76123,50)
        r={'gross':1.,'fees':.1,'friction':.2,'funding':-.03,'net':.67,'entry_notional':50,'hold_minutes':29}
        self.assertAlmostEqual(s.summarize([r])['net'],.67)

    def test_later_execution_and_funding(self):
        n=12000;a=np.full((n,2),100.); m=np.full((n,4),100.);signal=s.START+11000*s.MINUTE
        times={'signal':signal,'close':signal+30*s.MINUTE}
        events=[(signal-60*s.MINUTE,.0001),(signal+10*s.MINUTE,.0001),(signal+7*60*s.MINUTE,.0001)]
        result=s.simulate(a,m,events,None,times,'SHORT',1000,'BTCUSDT',False)
        self.assertNotIn('rejection',result)
        self.assertGreater(result['entry_ms'],result['availability_ms'])
        self.assertGreater(result['funding'],0)
        self.assertAlmostEqual(result['gross']-result['fees']-result['friction']+result['funding'],result['net'])

    def test_funding_gap_refuses_and_stale_risk_refuses(self):
        n=12000;a=np.full((n,2),100.);m=np.full((n,4),100.);signal=s.START+11000*s.MINUTE
        times={'signal':signal,'close':signal+30*s.MINUTE}
        r=s.simulate(a,m,[],None,times,'LONG',1000,'BTCUSDT',False)
        self.assertEqual(r['rejection'],'funding_history_incomplete')
        a[20,1]=np.nan
        times={'signal':s.START+10100*s.MINUTE,'close':s.START+10130*s.MINUTE}
        self.assertIsNone(s.risk_move(a,times['signal']))

    def test_future_gap_is_unresolved_not_a_prior_rejection(self):
        a=np.full((12000,2),100.);m=np.full((12000,4),100.);signal=s.START+11000*s.MINUTE
        times={'signal':signal,'close':signal+30*s.MINUTE}
        m[s.idx(signal+20*s.MINUTE)]=np.nan
        r=s.simulate(a,m,[],None,times,'LONG',1000,'BTCUSDT',False)
        self.assertEqual(r.get('unresolved'),'post_entry_trade_or_mark_gap')
        self.assertNotIn('rejection',r)

    def test_size_cap_not_silently_raised(self):
        a=np.full((12000,2),110001.);m=np.full((12000,4),110001.);signal=s.START+11000*s.MINUTE
        times={'signal':signal,'close':signal+30*s.MINUTE}
        r=s.simulate(a,m,[],None,times,'LONG',1000,'BTCUSDT',False)
        self.assertEqual(r.get('rejection'),'notional_cap')

    def test_cached_archive_parser_checks_pinned_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=pathlib.Path(tmp)/'a.zip'
            with zipfile.ZipFile(p,'w') as z:z.writestr('a.csv','1,2,3\n')
            item={'file':'a.zip','sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
            self.assertEqual(s.archive_rows(pathlib.Path(tmp),item)[0],[['1','2','3']])
            item['sha256']='0'*64
            with self.assertRaises(ValueError):s.archive_rows(pathlib.Path(tmp),item)

if __name__=='__main__':unittest.main()
