import hashlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from research.adaptive_shadow import canonical,PERIOD
from research.adaptive_ai_executor import run,APP_ID

class ExecutorTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.p=Path(self.t.name);(self.p/'interactive-once.lock').write_text('old')
  db=sqlite3.connect(self.p/'experiment-budget.db');db.execute(f'pragma application_id={APP_ID}');db.execute('create table api_attempts(request_id text primary key,reserved_micro_usd integer,created_at real,result_json text)');db.execute('create table api_usage(request_id text primary key,usage_json text)');db.execute('create table api_diagnostics(request_id text primary key,details_json text)');db.execute('insert into api_attempts values(?,?,?,?)',('original',3630,1,None));db.commit();db.close()
  self.now=10*PERIOD+2000;self.snap={'symbol':'BTCUSDT','as_of_ms':self.now-1000,'received_ms':self.now-1000,'latest_4h_ms':9*PERIOD,'latest_1h_ms':0,'latest_15m_ms':0,'features':{},'book':{},'news':[],'coverage':'incomplete'};self.snap['snapshot_id']=hashlib.sha256(canonical(self.snap).encode()).hexdigest()
 def tearDown(self):self.t.cleanup()
 def response(self,request):
  d={'mode':'TREND_DOWN','snapshot_id':self.snap['snapshot_id'],'data_ms':self.snap['as_of_ms'],'expires_ms':self.now+100000,'reasons':['trend'],'uncertainties':['missing depth timestamp']}
  return {'status':'completed','usage':{'input_tokens':100,'output_tokens':50},'output':[{'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':json.dumps(d)}]}]}
 def test_success_and_no_repeat(self):
  result=run(self.snap,'RANGE',self.p,transport=self.response,clock=lambda:self.now);self.assertEqual(result['baseline'],'RANGE');self.assertEqual(result['response']['mode'],'TREND_DOWN');self.assertLessEqual(result['reserved_micro_usd'],3640)
  with self.assertRaisesRegex(ValueError,'already_exists'):run(self.snap,'RANGE',self.p,transport=self.response,clock=lambda:self.now)
  db=sqlite3.connect(self.p/'experiment-budget.db');self.assertEqual(db.execute('select count(*) from api_attempts').fetchone()[0],2);self.assertEqual(db.execute('select count(*) from api_usage').fetchone()[0],1);db.close()
 def test_failure_retains_reservation_and_blocks_retry(self):
  def fail(req):raise RuntimeError('not a provider body')
  with self.assertRaises(RuntimeError):run(self.snap,'RANGE',self.p,transport=fail,clock=lambda:self.now)
  with self.assertRaisesRegex(ValueError,'already_exists'):run(self.snap,'RANGE',self.p,transport=self.response,clock=lambda:self.now)
  db=sqlite3.connect(self.p/'experiment-budget.db');self.assertEqual(db.execute('select count(*) from api_attempts where result_json is null').fetchone()[0],2);self.assertNotIn('provider body',db.execute('select details_json from api_diagnostics').fetchone()[0]);db.close()
 def test_stale_and_wrong_snapshot_never_reserve(self):
  with self.assertRaisesRegex(ValueError,'stale'):run(self.snap,'RANGE',self.p,transport=self.response,clock=lambda:self.now+300000)
  self.snap['symbol']='ETHUSDT'
  with self.assertRaisesRegex(ValueError,'hash'):run(self.snap,'RANGE',self.p,transport=self.response,clock=lambda:self.now)
  db=sqlite3.connect(self.p/'experiment-budget.db');self.assertEqual(db.execute('select count(*) from api_attempts').fetchone()[0],1);db.close()
 def test_missing_original_lock_fails_closed(self):
  (self.p/'interactive-once.lock').unlink()
  with self.assertRaisesRegex(ValueError,'original_budget'):run(self.snap,'RANGE',self.p,transport=self.response,clock=lambda:self.now)
 def test_claims_shared_gate_without_removing_original(self):
  from unittest.mock import patch
  with patch('research.adaptive_ai_executor._send',self.response):run(self.snap,'RANGE',self.p,clock=lambda:self.now)
  self.assertTrue((self.p/'interactive-once.lock').exists());self.assertTrue((self.p/'approved-followup-once.lock').exists())
  with self.assertRaisesRegex(ValueError,'gate_already_claimed'):run(self.snap,'RANGE',self.p,clock=lambda:self.now)
 def test_export_labels_baseline_and_rejects_expired(self):
  from research.adaptive_ai_export import export
  run(self.snap,'RANGE',self.p,transport=self.response,clock=lambda:self.now)
  r=export(self.p/'experiment-budget.db',self.p/'report.json',self.now);self.assertIn('baseline того же среза: RANGE',r['summary']);self.assertEqual(r['expires_utc'],__import__('datetime').datetime.fromtimestamp((self.now+100000)/1000,__import__('datetime').timezone.utc).isoformat())
  with self.assertRaisesRegex(ValueError,'expired'):export(self.p/'experiment-budget.db',self.p/'report.json',self.now+100000)
 def test_small_source_clock_offset_is_not_false_future(self):
  self.snap['as_of_ms']=self.now+200;self.snap['received_ms']=self.now;self.snap['snapshot_id']=hashlib.sha256(canonical({k:v for k,v in self.snap.items() if k!='snapshot_id'}).encode()).hexdigest()
  result=run(self.snap,'RANGE',self.p,transport=self.response,clock=lambda:self.now+300);self.assertEqual(result['baseline'],'RANGE')
 def test_export_failure_is_incomplete_and_does_not_leak_error(self):
  from research.adaptive_ai_export import export
  def fail(req):raise RuntimeError('private provider body')
  with self.assertRaises(RuntimeError):run(self.snap,'RANGE',self.p,transport=fail,clock=lambda:self.now)
  r=export(self.p/'experiment-budget.db',self.p/'report.json',self.now);self.assertFalse(r['complete']);self.assertIn('ответа нет',r['summary']);self.assertNotIn('private',json.dumps(r))
