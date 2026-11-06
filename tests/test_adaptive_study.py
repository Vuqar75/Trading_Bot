import unittest
from research import adaptive_study as a
class AdaptiveStudyTest(unittest.TestCase):
 def candles(self,n):return [(i*a.BAR,100.,101.,99.,100.) for i in range(n)]
 def test_signal_excludes_future(self):
  rows=self.candles(220);rows[195]=(rows[195][0],100,105,99,104);before=a.signals(rows,'trend1');changed=list(rows);changed[199]=(changed[199][0],100,150,99,140);after=a.signals(changed,'trend1');self.assertEqual(before[:199],after[:199]);self.assertEqual(before[195][0],'TREND')
 def test_stop_before_target_and_delayed_entry(self):
  rows=self.candles(12);signals=[None]*12;signals[0]=('BUY',98.,102.,101.);rows[2]=(2*a.BAR,100.,103.,98.,100.)
  out=a.simulate(rows,signals,['RANGE']*12,0,12*a.BAR,variant='range',impact_bps=5)
  self.assertEqual(out['closed'],1);self.assertEqual(out['exit_reasons']['stop'],1);self.assertLess(out['net_marked'],0);self.assertEqual(out['max_entry_wait_minutes'],15)
 def test_stale_missing_regime_no_combined_entry(self):
  rows=self.candles(8);signals=[None]*8;signals[0]=('BUY',98.,102.,101.)
  out=a.simulate(rows,signals,['NO_TRADE']*8,0,8*a.BAR,variant='range',combined=True);self.assertEqual(out['closed'],0);self.assertFalse(out['open_position'])
 def test_combined_uses_futures_prices_and_one_position(self):
  spot=self.candles(8);future=[(r[0],200.,201.,199.,200.) for r in spot];long=[None]*8;short=[None]*8;range_sig=[None]*8
  short[0]=('TREND',90.,100.,95.,90.,105.);range_sig[0]=('BUY',98.,102.,101.)
  result=a.simulate(spot,long,['TREND_DOWN']*8,0,8*a.BAR,variant='trend1',side='BOTH',combined=True,other_rows=future,other_intents=short,range_intents=range_sig,funding={3*a.BAR:[(3*a.BAR,.01,200.)]})
  self.assertTrue(result['open_position']);self.assertGreater(result['turnover'],9.9);self.assertGreater(result['funding'],.09)
 def test_independent_long_trade_cost_arithmetic(self):
  rows=self.candles(8)
  for i in (3,4,5,6,7):rows[i]=(i*a.BAR,110.,111.,109.,110.)
  signals=[None]*8;signals[0]=('TREND',101.,100.,90.,95.,105.);signals[3]=('TREND',90.,120.,80.,95.,130.)
  result=a.simulate(rows,signals,['TREND_UP']*8,0,8*a.BAR,variant='trend1',side='LONG',impact_bps=5)
  self.assertEqual(result['closed'],1);self.assertAlmostEqual(result['realized_net'],result['gross_closed']-result['fees']-result['impact'],places=9);self.assertGreater(result['realized_net'],0)
 def test_range_break_during_latency_cancels_entry(self):
  rows=self.candles(8);rows[1]=(a.BAR,100.,103.,99.,100.);signals=[None]*8;signals[0]=('BUY',98.,102.,101.)
  result=a.simulate(rows,signals,['RANGE']*8,0,8*a.BAR,variant='range');self.assertFalse(result['open_position']);self.assertEqual(result['rejected_entries'],1)
