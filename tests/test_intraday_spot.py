import unittest
from research.intraday_spot import BAR,aggregate,aggregate_minutes,calculate,simulate,baseline,require_closed

def candle(i,o=100,h=101,l=99,c=100):return (i*BAR,o,h,l,c,1.)

class IntradaySpotTests(unittest.TestCase):
 def test_aggregate_requires_complete_contiguous_utc_group(self):
  out=aggregate([candle(0),candle(1,101,103,100,102),candle(2,102,104,101,103)],3)
  self.assertEqual(out,[(0,100,104,99,103,3.)])
  with self.assertRaises(ValueError):aggregate([candle(0),candle(2),candle(3)],3)
  with self.assertRaises(ValueError):aggregate([candle(0),candle(1)],3)
  minute=[(i*60000,100.,101.,99.,100.,1.) for i in range(15)]
  five=aggregate_minutes(minute,60000,5);fifteen=aggregate(five,3)
  self.assertEqual((len(five),len(fifteen),fifteen[0][-1]),(3,1,15.))
  with self.assertRaises(ValueError):aggregate_minutes(minute[:-1],60000,5)
 def test_future_change_does_not_change_prior_signals(self):
  rows=[candle(i,100,100.5,99.5,100) for i in range(300)]
  changed=rows.copy();changed[270]=candle(270,100,110,90,105)
  for factor in (1,3):
   a=calculate(rows,factor);b=calculate(changed,factor)
   self.assertEqual({t:v for t,v in a.items() if t<=270*BAR},{t:v for t,v in b.items() if t<=270*BAR})
 def test_windows_cover_same_duration(self):
  self.assertEqual(72*5,24*15);self.assertEqual(36*5,12*15);self.assertEqual(144*5,48*15)
  data=[candle(i,100,101,99,100) for i in range(300)]
  for factor in (1,3):self.assertFalse(any(x['break_buy'] for x in calculate(data,factor).values()))
 def test_future_period_rejected(self):
  with self.assertRaisesRegex(ValueError,'not fully closed'):require_closed(99,100)
  require_closed(100,100)
 def test_signal_executes_next_open_and_delay_changes_price(self):
  rows=[candle(0),candle(1,100,101,99.9,100),candle(2,101,102,100.9,101),candle(3,101,102,100.9,101)]
  sig={BAR:{'break_buy':True,'break_sell':False,'regime':'range','lo':90,'mid':105,'hi':110}}
  a=simulate(rows,sig,'BTCUSDT',0,4*BAR,'breakout',impact=0,delay=0)
  b=simulate(rows,sig,'BTCUSDT',0,4*BAR,'breakout',impact=0,delay=1)
  self.assertEqual(a['entries'],1);self.assertEqual(b['entries'],1)
  self.assertAlmostEqual(a['net'],.1-(10+10.1)*.001,places=7)
  self.assertLess(b['net'],a['net'])
  self.assertEqual((a['execution_delay_minutes'],b['execution_delay_minutes']),(0,5))
 def test_stop_wins_against_target_in_same_bar(self):
  rows=[candle(0),candle(1,100,102,98,100),candle(2)]
  sig={BAR:{'range_buy':True,'regime':'range','lo':95,'mid':101,'hi':105}}
  r=simulate(rows,sig,'BTCUSDT',0,3*BAR,'range',impact=0,delay=0)
  self.assertEqual(r['exit_reasons'],{'stop':1});self.assertEqual(r['ambiguous_stop_target'],1)
  self.assertAlmostEqual(r['gross'],-.075,places=8);self.assertLess(r['net'],r['gross'])
 def test_rounding_and_baseline_fees(self):
  rows=[candle(0,100,101,99,100),candle(1,110,111,109,110)]
  r=baseline(rows,'BTCUSDT',0,2*BAR,0,0)
  self.assertAlmostEqual(r['buy_hold_net'],1-.021,places=8)
  self.assertTrue(r['open_marked'])
 def test_target_touch_is_not_filled_at_the_target_price(self):
  rows=[candle(0),candle(1,100,102,99.5,101),candle(2,100.5,101,100,100.5)]
  sig={BAR:{'range_buy':True,'regime':'range','lo':95,'mid':101,'hi':105}}
  r=simulate(rows,sig,'BTCUSDT',0,3*BAR,'range',impact=0,delay=0)
  self.assertEqual(r['exit_reasons'],{'target':1})
  self.assertAlmostEqual(r['gross'],.05,places=8)
 def test_range_rejects_gap_outside_saved_range(self):
  rows=[candle(0),candle(1,106,107,105,106),candle(2)]
  sig={BAR:{'range_buy':True,'regime':'range','lo':95,'mid':101,'hi':105}}
  r=simulate(rows,sig,'BTCUSDT',0,3*BAR,'range',impact=0,delay=0)
  self.assertEqual(r['entries'],0);self.assertEqual(r['rejected']['range_or_distance_at_fill'],1)
 def test_target_distance_uses_cost_adjusted_entry(self):
  rows=[candle(0),candle(1,100,101,99.5,100),candle(2)]
  sig={BAR:{'range_buy':True,'regime':'range','lo':95,'mid':100.62,'hi':105}}
  r=simulate(rows,sig,'BTCUSDT',0,3*BAR,'range',impact=5,delay=0)
  self.assertEqual(r['entries'],0);self.assertEqual(r['rejected']['range_or_distance_at_fill'],1)

if __name__=='__main__':unittest.main()
