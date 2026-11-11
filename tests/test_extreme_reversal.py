import math,unittest
import numpy as np
from unittest.mock import patch
from research import extreme_reversal as e

class ExtremeReversalTest(unittest.TestCase):
 def test_sigma_excludes_signal_bar_and_gaps(self):
  bars=450;raw=np.full((bars*120,4),100.);r=np.array([.001 if i%2 else -.001 for i in range(bars)])
  closes=100*np.exp(np.cumsum(r));r[400]=-.02;closes=100*np.exp(np.cumsum(r))
  for i,c in enumerate(closes):raw[i*120:(i+1)*120]=c
  sig,invalid=e.build_signals(raw);target=e.START+401*120*e.M
  hit=next(x for x in sig if x['signal_ms']==target)
  self.assertEqual(hit['direction'],'LONG');self.assertAlmostEqual(hit['sigma'],np.std(r[40:400],ddof=1),places=12)
  raw[399*120,0]=np.nan;sig2,_=e.build_signals(raw)
  self.assertFalse(any(x['signal_ms']==target for x in sig2))

 def test_short_gap_stop_and_arithmetic(self):
  a=np.full((400,4),100.);m=a.copy();s={'signal_ms':e.START+100*e.M,'direction':'SHORT'}
  # Entry at 101; next open gaps above 2% stop.
  a[102]=[104,105,103,104];m[102]=[104,105,103,104]
  acct={'cash':1000.,'peak':1000.,'max_dd':0.}
  t=e.execute(s,a,m,{},acct,'BTCUSDT',False)
  self.assertEqual(t['reason'],'stop');self.assertEqual(t['exit_reference'],104)
  self.assertLess(t['net'],0);self.assertAlmostEqual(t['gross']-t['fees']-t['impact']-t['liquidation_fee']+t['funding'],t['net'])

 def test_funding_direction_and_time_exit(self):
  a=np.full((400,4),100.);m=a.copy();s={'signal_ms':e.START+100*e.M,'direction':'SHORT'}
  acct={'cash':1000.,'peak':1000.,'max_dd':0.};ft=e.START+110*e.M+1
  t=e.execute(s,a,m,{110:[(ft,.001)]},acct,'BTCUSDT',False)
  self.assertEqual(t['reason'],'time');self.assertEqual(t['hold_min'],120);self.assertGreater(t['funding'],0);self.assertEqual(t['funding_events'][0]['event_ms'],ft)

 def test_stop_funding_order_uncertain_credit_is_not_assumed(self):
  a=np.full((400,4),100.);m=a.copy();s={'signal_ms':e.START+100*e.M,'direction':'SHORT'}
  a[110]=[100,104,99,103];m[110]=[100,104,99,103]
  t=e.execute(s,a,m,{110:[(e.START+110*e.M+1,.001)]},{'cash':1000.,'peak':1000.,'max_dd':0.},'BTCUSDT',False)
  self.assertEqual(t['reason'],'stop');self.assertEqual(t['funding'],0);self.assertEqual(t['funding_ambiguous'],1)

 def test_floor_and_reject(self):
  q,err=e.size_for(99999,99999,'BTCUSDT',.0005)
  self.assertIsNone(err);self.assertLessEqual(q*99999,100)
  self.assertEqual(e.size_for(100001,100001,'BTCUSDT',.0005)[1],'min_qty_or_cap')

 def test_open_at_end_is_marked_not_fabricated_close(self):
  a=np.full((200,4),100.);s={'signal_ms':e.START+100*e.M,'direction':'LONG'}
  with patch.object(e,'N',200),patch.object(e,'END',e.START+200*e.M):
   t=e.execute(s,a,a,{}, {'cash':1000.,'peak':1000.,'max_dd':0.},'BTCUSDT',False)
  self.assertEqual(t['unresolved'],'open_at_end');self.assertNotIn('exit_ms',t)
  self.assertIsNotNone(t['unrealized_net_estimate'])

if __name__=='__main__':unittest.main()
