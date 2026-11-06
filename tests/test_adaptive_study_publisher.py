import copy,json,os,tempfile,unittest
from pathlib import Path
from research.adaptive_decision_export import build,stage
from research.adaptive_study_publisher import publish
from tests import test_adaptive_decision_export as fixture

class PublisherTest(unittest.TestCase):
 def setUp(self):
  self.f=fixture.DecisionExportTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.private=self.root/'private';self.private.mkdir(mode=0o700);self.public=self.root/'public';self.public.mkdir(mode=0o750)
 def write(self,name,value):
  p=self.private/name;p.write_text(json.dumps(value));p.chmod(0o600);return p
 def pub(self,src,target,kind):return publish(src,target,source_uid=os.getuid(),target_gid=os.getgid(),kind=kind)
 def test_ai_and_baseline_are_distinct_read_only_publications(self):
  stage(self.f.path,self.f.capture);ai=build(self.f.db,self.f.path,'r')
  b=self.write('baseline',self.f.capture);a=self.write('ai',ai)
  out_b=self.public/'baseline.json';out_a=self.public/'ai.json'
  self.assertTrue(self.pub(b,out_b,'baseline')['published']);self.assertTrue(self.pub(a,out_a,'AI')['published'])
  self.assertFalse(self.pub(a,out_a,'AI')['published'])
  self.assertEqual(json.loads(out_a.read_text())['decision']['mode'],'NO_TRADE')
  self.assertNotIn('decision',json.loads(out_b.read_text()))
  self.assertEqual(out_a.stat().st_mode&0o777,0o640)
 def test_untrusted_source_and_conflicting_same_time_fail(self):
  source=self.write('baseline',self.f.capture);target=self.public/'baseline.json'
  self.pub(source,target,'baseline');source.chmod(0o666)
  with self.assertRaisesRegex(ValueError,'untrusted_publisher_file'):self.pub(source,target,'baseline')
  source.chmod(0o600);changed=copy.deepcopy(self.f.capture);changed['candles']['15m'][-1][1]='99.5';source.write_text(json.dumps(changed))
  with self.assertRaisesRegex(ValueError,'publication_conflict'):self.pub(source,target,'baseline')
