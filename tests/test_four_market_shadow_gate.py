"""Shadow-mode transition evidence; no model, network, or trading runtime."""
import os,socket,unittest
from research.adaptive_shadow import PERIOD,gate,validate_model

class ShadowTransitions(unittest.TestCase):
 def test_bench_blocks_ip_and_does_not_inherit_credentials(self):
  with self.assertRaisesRegex(OSError,'IP sockets forbidden'):socket.socket(socket.AF_INET,socket.SOCK_STREAM)
  for key in ('TRADING_BOT_AI','OPENAI_API_KEY','RUNTIME_TELEGRAM_BOT_TOKEN','RUNTIME_TELEGRAM_OWNER_ID'):
   self.assertNotIn(key,os.environ)
 def test_all_regimes_and_transitions_remain_shadow_only(self):
  now=20_000*PERIOD
  for mode in ('TREND_UP','RANGE','TREND_DOWN','NO_TRADE','TREND_UP'):
   result={'mode':mode,'snapshot_id':'fixture','data_ms':now,'expires_ms':now+PERIOD,'reasons':['fixture'],'uncertainties':['fixture']}
   validate_model(result,'fixture',now,now+1)
   self.assertFalse(gate(result,data_healthy=True,budget_ready=True,eligible=True)['allow'])
   self.assertFalse(gate(result,data_healthy=False,budget_ready=True,eligible=True)['allow'])
   self.assertFalse(gate(result,data_healthy=True,budget_ready=False,eligible=True)['allow'])
   self.assertFalse(gate(result,data_healthy=True,budget_ready=True,eligible=False)['allow'])
 def test_stale_or_corrupt_results_are_rejected(self):
  now=20_000*PERIOD
  result={'mode':'TREND_UP','snapshot_id':'fixture','data_ms':now,'expires_ms':now+1000,'reasons':['fixture'],'uncertainties':['fixture']}
  for value,when in ((result,now+1000),(dict(result,mode='BUY'),now),(dict(result,snapshot_id='other'),now)):
   with self.assertRaises(ValueError):validate_model(value,'fixture',now,when)
