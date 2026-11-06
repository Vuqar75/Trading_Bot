import copy,sqlite3,unittest
from research.adaptive_snapshot_proof import bind
from research.adaptive_study_observations import save
from tests import test_adaptive_decision_export as fixture

class ObservationTest(unittest.TestCase):
 def setUp(self):
  self.f=fixture.DecisionExportTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.ai=sqlite3.connect(':memory:',isolation_level=None);self.base=sqlite3.connect(':memory:',isolation_level=None)
  self.addCleanup(self.ai.close);self.addCleanup(self.base.close)
 def test_cost_once_and_baseline_independent(self):
  f=self.f;doc=bind(f.capture,f.decision,f.now+1,model='gpt-6-luna',request_sha256='0'*64,usage={'input_tokens':1000,'output_tokens':200})
  first=save(self.ai,doc,source='AI',now_ms=f.now+2,start_ms=f.now//14400000*14400000-1)
  self.assertEqual(first['estimated_api_usd'],'0.0002');self.assertFalse(first['provider_bill_confirmed'])
  self.assertTrue(save(self.ai,doc,source='AI',now_ms=f.now+2,start_ms=f.now//14400000*14400000-1)['replayed'])
  baseline=save(self.base,f.capture,source='baseline',now_ms=f.now+2,start_ms=f.now//14400000*14400000-1)
  self.assertEqual(baseline['mode'],'RANGE');self.assertIsNone(baseline['estimated_api_usd'])
 def test_unknown_usage_never_becomes_zero(self):
  f=self.f;doc=bind(f.capture,f.decision,f.now+1,model='gpt-6-luna',request_sha256='0'*64)
  self.assertIsNone(save(self.ai,doc,source='AI',now_ms=f.now+2,start_ms=f.now//14400000*14400000-1)['estimated_api_usd'])
