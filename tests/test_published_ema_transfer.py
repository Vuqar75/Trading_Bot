import unittest
import numpy as np
from research import published_ema_transfer as e

class PublishedEMATest(unittest.TestCase):
 def test_author_saved_training_choice(self):
  x=np.load('research/published_ema_review/author-sample-train.npz')['pnl']
  k,s=e.select_train(x);self.assertEqual(e.PAIRS[k],(20,50));self.assertAlmostEqual(s[k],.7077856309759551)
 def test_seed_lag_and_tie(self):
  close=np.arange(1.,241.);d,p=e.author_matrices(close)
  self.assertAlmostEqual(e.ema_prices(close)[5][4],3.)
  self.assertTrue(np.isnan(d[198]).all());self.assertTrue(np.isfinite(d[199]).all())
  self.assertTrue(np.all(d[199]==1));self.assertTrue(np.all(p[199]==0))
  v=np.tile(np.arange(1.,11.)[:,None],(1,30));self.assertEqual(e.select_train(v)[0],0)
 def test_switch_pair_same_exposure_and_reversal_fees(self):
  a=e.Account('BTCUSDT',False);T=e.BEGIN;bar=np.array([100.,100.5,99.5,100.,1.]);mark=bar[:4]
  a.signal(T,1,[5,40]);a.tick(T+e.M,bar,mark,[]);cash=a.cash;qty=a.position['qty']
  a.signal(T+e.H,1,[10,50]);self.assertEqual(a.cash,cash);self.assertEqual(a.position['qty'],qty);self.assertIsNone(a.pending)
  a.signal(T+2*e.H,-1,[10,50]);a.tick(T+2*e.H+e.M,bar,mark,[])
  self.assertEqual(len(a.trades),1);self.assertEqual(a.position['sign'],-1)
  self.assertEqual([x['action'] for x in a.events],['entry','exit','entry'])
  self.assertGreater(a.trades[0]['fees'],0);self.assertGreater(a.position['entry_fee'],0)
 def test_stop_cancels_pre_stop_intent_and_waits_for_later_hour(self):
  a=e.Account('BTCUSDT',False);T=e.BEGIN;bar=np.array([100.,100.5,99.5,100.,1.]);a.signal(T,1,[5,40]);a.tick(T+e.M,bar,bar[:4],[])
  a.signal(T+e.H,-1,[5,40]);gap=np.array([97.,98.,96.,97.,1.]);a.tick(T+e.H,gap,gap[:4],[])
  self.assertIsNone(a.position);self.assertIsNone(a.pending);self.assertEqual(a.trades[0]['reason'],'stop')
  a.signal(T+e.H+e.M,-1,[5,40]);self.assertIsNone(a.pending)
  a.signal(T+2*e.H,-1,[5,40]);self.assertIsNotNone(a.pending)
 def test_funding_and_cash_reconcile(self):
  a=e.Account('BTCUSDT',False);T=e.BEGIN;bar=np.array([100.,100.5,99.5,100.,1.]);a.signal(T,-1,[5,40]);a.tick(T+e.M,bar,bar[:4],[])
  a.tick(T+2*e.M,bar,bar[:4],[(T+2*e.M+1,.001)])
  self.assertGreater(a.position['funding'],0);a.close(T+3*e.M,100.,'signal_reversal')
  self.assertAlmostEqual(a.cash,1000+a.trades[0]['net'])

 def test_open_position_is_marked_and_missing_data_halts(self):
  a=e.Account('BTCUSDT',False);T=e.BEGIN;bar=np.array([100.,100.5,99.5,100.,1.]);a.signal(T,1,[5,40]);a.tick(T+e.M,bar,bar[:4],[])
  z=a.finish(100.);self.assertIsNotNone(z['open']);self.assertEqual(z['closed'],0)
  self.assertAlmostEqual(z['equity'],1000+z['open']['unrealized_net_estimate'])
  broken=np.array([np.nan]*4);a.tick(T+2*e.M,bar,broken,[])
  self.assertEqual(a.halted['reason'],'price_or_mark_missing');self.assertIsNotNone(a.position)

 def test_actual_stitched_transition_and_final_close_cost(self):
  first=(e.BEGIN-e.START)//e.H-1;closes=np.full(first+4,100.)
  r=e.diagnostic(closes,np.array([1,1,-1]))
  self.assertAlmostEqual(r['actual_transition_cost'],.003)
  self.assertAlmostEqual(r['final_close_cost'],.001)
  self.assertAlmostEqual(r['log_net_after_final_close'],-.004)

 def test_first_choice_never_uses_future_closes(self):
  n=(e.END-e.START)//e.H;indices=np.arange(n);c=100+indices*.001+np.sin(indices/7)*.3
  _,_,a=e.selected_path(c,'14d_10d','BTCUSDT')
  altered=c.copy();first=(e.BEGIN-e.START)//e.H-1;altered[first+1:first+200]*=1.2
  _,_,b=e.selected_path(altered,'14d_10d','BTCUSDT')
  self.assertEqual(a[0]['selected_pair'],b[0]['selected_pair']);self.assertEqual(a[0]['sharpe'],b[0]['sharpe'])
  self.assertEqual(a[0]['train_end_ms'],e.BEGIN);self.assertEqual(a[0]['test_start_ms'],e.BEGIN)
  self.assertEqual(a[0]['train_end_ms']-a[0]['train_start_ms'],14*24*e.H)
  self.assertEqual(a[0]['first_training_close_ms'],a[0]['train_start_ms']+e.H)

if __name__=='__main__':unittest.main()
