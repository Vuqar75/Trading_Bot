import copy,hashlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from research.adaptive_decision_export import stage,build,export_latest,export_budget
from research.adaptive_snapshot_proof import capture
from research.adaptive_shadow import PERIOD
from research.adaptive_shadow import canonical
from research.adaptive_regular import regime_request,source_snapshot
from tests import test_adaptive_shadow as shadow_fixture

class DecisionExportTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)
  self.f=shadow_fixture.ShadowTest();self.f.setUp();self.addCleanup(self.f.doCleanups);self.now=self.f.now
  self.capture=capture(self.f.fetch,clock=lambda:self.now,news_fetch=lambda **kw:([],[]));snapshot=self.capture['snapshot']
  self.decision={'mode':'NO_TRADE','snapshot_id':snapshot['snapshot_id'],'data_ms':self.now,'expires_ms':self.now+10000,'reasons':['fixture'],'uncertainties':['fixture']}
  self.db=sqlite3.connect(':memory:',isolation_level=None);self.addCleanup(self.db.close)
  self.db.executescript('CREATE TABLE regular_windows(boundary_ms INTEGER,state TEXT,request_id TEXT);CREATE TABLE adaptive_decisions(request_id TEXT,snapshot_json TEXT,baseline TEXT,decision_json TEXT);CREATE TABLE regular_versions(boundary_ms INTEGER,protocol TEXT,model TEXT,request_sha TEXT,snapshot_id TEXT,baseline_version TEXT);CREATE TABLE adaptive_responses(request_id TEXT,response_json TEXT,received_ms INTEGER);')
  self.db.execute('INSERT INTO regular_windows VALUES(?,?,?)',(self.now//PERIOD*PERIOD,'completed','r'))
  self.db.execute('INSERT INTO adaptive_decisions VALUES(?,?,?,?)',('r',json.dumps(snapshot),self.capture['baseline'],json.dumps({'model':'gpt-6-luna','response':self.decision,'baseline':self.capture['baseline'],'received_ms':self.now+1,'usage':None})))
  self.db.execute('INSERT INTO adaptive_responses VALUES(?,?,?)',('r',json.dumps({'status':'completed','output':[{'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':json.dumps(self.decision)}]}]}),self.now+1))
  boundary=self.now//PERIOD*PERIOD
  req_hash=hashlib.sha256(canonical(regime_request(snapshot,boundary)).encode()).hexdigest()
  self.db.execute('INSERT INTO regular_versions VALUES(?,?,?,?,?,?)',(boundary,'regular-regime-horizon-v2','gpt-6-luna',req_hash,snapshot['snapshot_id'],'sma20-4h-1pct-v1'))
 def test_bound_to_completed_decision_and_exact_staged_snapshot(self):
  p=stage(self.path,self.capture);self.assertEqual(stage(self.path,self.capture),p)
  self.assertEqual(build(self.db,self.path,'r')['decision']['mode'],'NO_TRADE')
  self.assertEqual(export_latest(self.db,self.path),self.capture['snapshot']['snapshot_id'])
  self.assertEqual(json.loads((self.path/'ai-current.json').read_text())['decision']['mode'],'NO_TRADE')
  self.db.execute("UPDATE regular_windows SET state='reserved'")
  with self.assertRaisesRegex(ValueError,'durable_decision_missing'):build(self.db,self.path,'r')
 def test_same_snapshot_conflicting_captured_candles_not_overwritten(self):
  stage(self.path,self.capture);changed=copy.deepcopy(self.capture);changed['candles']['4h'][-2][4]='100.5'
  with self.assertRaisesRegex(ValueError,'capture_identity_conflict'):stage(self.path,changed)
 def test_old_without_recorded_raw_candles_cannot_be_reconstructed(self):
  with self.assertRaisesRegex(ValueError,'capture_missing'):build(self.db,self.path,'r')
 def test_wrong_request_hash_is_rejected(self):
  stage(self.path,self.capture);self.db.execute("UPDATE regular_versions SET request_sha=?",('0'*64,))
  with self.assertRaisesRegex(ValueError,'request_hash_mismatch'):build(self.db,self.path,'r')
 def test_saved_response_cannot_be_replaced_by_forged_decision(self):
  stage(self.path,self.capture);row=json.loads(self.db.execute('SELECT decision_json FROM adaptive_decisions').fetchone()[0]);row['response']['mode']='TREND_UP'
  self.db.execute('UPDATE adaptive_decisions SET decision_json=?',(json.dumps(row),))
  with self.assertRaisesRegex(ValueError,'durable_response_mismatch'):build(self.db,self.path,'r')
 def test_opt_in_source_stages_evidence_before_return(self):
  snapshot,base=source_snapshot(lambda:None,lambda:self.capture,self.path)
  self.assertEqual(snapshot,self.capture['snapshot']);self.assertEqual(base,self.capture['baseline'])
  self.assertTrue((self.path/(snapshot['snapshot_id']+'.json')).is_file())
  self.assertTrue((self.path/'baseline-current.json').is_file())
  self.assertEqual(source_snapshot(lambda:('old','baseline'),None,None),('old','baseline'))
  with self.assertRaisesRegex(ValueError,'capture_directory_required'):source_snapshot(lambda:None,lambda:self.capture,None)
 def test_budget_export_keeps_unknown_reservations_separate(self):
  self.db.executescript('CREATE TABLE api_attempts(request_id TEXT,reserved_micro_usd INTEGER);CREATE TABLE api_usage(request_id TEXT,usage_json TEXT);')
  self.db.execute('INSERT INTO api_attempts VALUES(?,?)',('old',5718));self.db.execute('INSERT INTO api_attempts VALUES(?,?)',('known',2000))
  self.db.execute('INSERT INTO api_usage VALUES(?,?)',('known',json.dumps({'input_tokens':1000,'output_tokens':200})))
  value=export_budget(self.db,self.path,now_ms=self.now)
  self.assertEqual((value['reserved_micro_usd'],value['unknown_attempts'],value['known_estimated_usd']),(7718,1,'0.0002'))
  self.assertFalse(value['provider_bill_confirmed'])
  self.assertEqual(export_budget(self.db,self.path,now_ms=self.now+1000)['observed_ms'],self.now)
  self.db.execute('INSERT INTO api_attempts VALUES(?,?)',('new',100))
  self.assertEqual(export_budget(self.db,self.path,now_ms=self.now+2000)['reserved_micro_usd'],7818)
