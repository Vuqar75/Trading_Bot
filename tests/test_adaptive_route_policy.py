import unittest
from research.adaptive_route_policy import route,closed_bar_signal
from research.adaptive_shadow import PERIOD

class PolicyTest(unittest.TestCase):
 def setUp(self):
  self.now=20000*PERIOD
  self.d={'mode':'TREND_UP','snapshot_id':'fixture','data_ms':self.now,'expires_ms':self.now+PERIOD,'reasons':['fixture'],'uncertainties':['fixture']}
  self.kw={'snapshot_id':'fixture','data_ms':self.now,'now_ms':self.now+1,'symbol':'BTCUSDT','decision_version':'regular-regime-horizon-v2','eligible':True,'research_sandbox':True}
 def test_fixed_mapping_and_no_trade(self):
  for mode,market,strategy,direction in [('TREND_UP','spot','trend1','LONG'),('TREND_DOWN','futures','trend1','SHORT'),('RANGE','spot','range','LONG')]:
   r=route(dict(self.d,mode=mode),**self.kw);self.assertEqual((r['market'],r['strategy'],r['direction']),(market,strategy,direction))
  self.assertEqual(route(dict(self.d,mode='NO_TRADE'),**self.kw)['reason'],'no_trade')
 def test_fail_closed(self):
  for change,reason in [({'research_sandbox':False},'research_sandbox_required'),({'symbol':'ETHUSDT'},'unsupported_symbol'),({'decision_version':'unknown'},'unsupported_decision_version'),({'snapshot_id':'wrong'},'invalid_or_expired_decision'),({'eligible':False},'research_eligibility_missing'),({'market':'futures_cross'},'market_policy_absent'),({'market':'margin_cross'},'market_policy_absent'),({'now_ms':self.now+PERIOD},'invalid_or_expired_decision')]:
   self.assertEqual(route(self.d,**(self.kw|change))['reason'],reason)
 def test_real_fixed_hourly_signal_and_gap(self):
  bar=900000;rows=[(i*bar,100.,101.,99.,100.) for i in range(196)];rows[-1]=(195*bar,100.,102.,99.,101.)
  sig=closed_bar_signal(rows,'trend1',now_ms=196*bar);self.assertEqual(sig[0],'TREND');self.assertGreater(sig[1],sig[2])
  with self.assertRaisesRegex(ValueError,'unclosed'):closed_bar_signal(rows,'trend1',now_ms=196*bar-1)
  del rows[2]
  with self.assertRaisesRegex(ValueError,'gap'):closed_bar_signal(rows,'trend1',now_ms=196*bar)
