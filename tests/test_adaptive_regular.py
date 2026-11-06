import hashlib,json,sqlite3,tempfile,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from research import adaptive_regular as r
from research.adaptive_ai_executor import APP_ID,execute_reserved
from research.adaptive_shadow import PERIOD,canonical
class RegularTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.p=Path(self.t.name);self.public=self.p/'public'/'report.json';self.b=100*PERIOD;self.now=self.b+120001
  authorization=patch.object(r,'AUTH_PATH',self.p/'regular-authorization.json');authorization.start();self.addCleanup(authorization.stop)
  for name in ('interactive-once.lock','approved-followup-once.lock','adaptive-next-once.lock'):(self.p/name).write_text('old')
  (self.p/'server-owner.json').write_text(json.dumps({'role':'ACTIVE_SERVER','migration_id':'test'}));(self.p/'regular-authorization.json').write_text(json.dumps({'protocol':r.PROTOCOL,'start_ms':self.b,'max_daily':6,'cap_micro_usd':r.CAP,'max_reserve_micro_usd':r.MAX_RESERVE}))
  with closing(sqlite3.connect(self.p/'experiment-budget.db')) as d:
   d.execute(f'pragma application_id={APP_ID}');d.execute('create table api_attempts(request_id text primary key,reserved_micro_usd integer not null,created_at real not null,result_json text)');d.executemany('insert into api_attempts values(?,?,?,NULL)',[('old',3630,1),('previous',4176,2)]);d.commit()
  r.initialize(self.p)
  self.calls=[];self.make_snapshot()
 def make_snapshot(self):
  self.snap={'symbol':'BTCUSDT','as_of_ms':self.now-1000,'received_ms':self.now-1000,'latest_4h_ms':self.b-PERIOD,'latest_1h_ms':0,'latest_15m_ms':0,'features':{'close_4h':100},'book':{},'news':[],'coverage':'test'};self.snap['snapshot_id']=hashlib.sha256(canonical(self.snap).encode()).hexdigest()
 def fake(self,req):
  self.calls.append(1);d={'mode':'NO_TRADE','snapshot_id':self.snap['snapshot_id'],'data_ms':self.snap['as_of_ms'],'expires_ms':self.now+100000,'reasons':['test'],'uncertainties':['test']}
  return {'status':'completed','usage':{'input_tokens':100,'output_tokens':20},'output':[{'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':json.dumps(d)}]}]}
 def send(self,db,rid,req,clock):return execute_reserved(db,rid,req,transport=self.fake,clock=clock)
 def tick(self,**kwargs):
  opts={'clock':lambda:self.now,'fetch':lambda:(self.snap,'TREND_DOWN'),'secret':lambda:'fake','send':self.send};opts.update(kwargs);return r.tick(self.p,self.public,**opts)
 def test_success_same_snapshot_and_idempotent(self):
  out=self.tick();self.assertEqual((out['mode'],out['baseline']),('NO_TRADE','TREND_DOWN'));self.assertEqual(len(self.calls),1);self.assertEqual(self.tick()['status'],'completed');self.assertEqual(len(self.calls),1)
  d=json.loads(self.public.read_text());self.assertIn('baseline того же среза: TREND_DOWN',d['summary']);self.assertIn('входы запрещены',d['risks'])
 def test_opt_in_capture_is_durable_before_reserved_http_and_builds_export(self):
  from tests import test_adaptive_shadow as fixture
  from research.adaptive_snapshot_proof import capture
  from research.adaptive_decision_export import build
  source=fixture.ShadowTest();source.setUp();self.addCleanup(source.doCleanups);source.now=self.now
  captured=capture(source.fetch,clock=lambda:self.now,news_fetch=lambda **kw:([],[]));self.snap=captured['snapshot']
  directory=self.p/'evidence';directory.mkdir(mode=0o700)
  def send(db,rid,req,clock):
   self.assertTrue((directory/(self.snap['snapshot_id']+'.json')).is_file())
   return self.send(db,rid,req,clock)
  self.assertEqual(self.tick(capture_fetch=lambda:captured,evidence_dir=directory,send=send)['status'],'completed')
  with closing(sqlite3.connect(self.p/'experiment-budget.db')) as db:
   exported=build(db,directory,'regular:'+str(self.b))
  self.assertEqual((exported['decision']['mode'],exported['baseline']),('NO_TRADE','RANGE'))
  self.assertEqual(len(self.calls),1)
 def test_request_defines_regime_horizon_without_rewriting_source_or_one_shot(self):
  from research.adaptive_ai_executor import request
  original=canonical(self.snap);one_shot=canonical(request(self.snap))
  req=r.regime_request(self.snap,self.b);prompt=req['input'][0]['content']
  self.assertIn('review_ms='+str(self.b+PERIOD),prompt)
  self.assertIn('not a 15-minute entry signal',prompt)
  self.assertIn('earlier expiry',prompt);self.assertIn('plain Russian',prompt)
  self.assertEqual(canonical(self.snap),original)
  self.assertEqual(canonical(request(self.snap)),one_shot)
  self.assertEqual(req['text']['format']['schema'],request(self.snap)['text']['format']['schema'])
 def test_horizon_version_is_recorded_and_short_model_expiry_is_not_extended(self):
  before=canonical(self.snap);self.assertEqual(self.tick()['status'],'completed')
  with closing(sqlite3.connect(self.p/'experiment-budget.db')) as db:
   version,request_sha=db.execute('SELECT protocol,request_sha FROM regular_versions').fetchone()
   decision=json.loads(db.execute("SELECT decision_json FROM adaptive_decisions WHERE decision_json IS NOT NULL").fetchone()[0])
  self.assertEqual(version,r.DECISION_PROTOCOL)
  self.assertEqual(request_sha,hashlib.sha256(canonical(r.regime_request(self.snap,self.b)).encode()).hexdigest())
  self.assertEqual(decision['response']['expires_ms'],self.now+100000)
  self.assertEqual(canonical(self.snap),before);self.assertEqual(len(self.calls),1)
 def test_request_rejects_unaligned_or_wrong_closed_candle(self):
  with self.assertRaisesRegex(ValueError,'boundary_invalid'):r.regime_request(self.snap,self.b+1)
  with self.assertRaisesRegex(ValueError,'boundary_invalid'):r.regime_request(self.snap,self.b+PERIOD)
 def test_uncertain_pauses_future_boundaries(self):
  def fail(*a,**k):self.calls.append(1);raise TimeoutError()
  self.assertEqual(self.tick(send=fail)['reason'],'uncertain_http');self.b+=PERIOD;self.now=self.b+120001;self.make_snapshot();self.assertEqual(self.tick()['reason'],'uncertain_http');self.assertEqual(len(self.calls),1)
 def test_missed_and_stale_never_send(self):
  self.now=self.b+300001;self.assertEqual(self.tick()['status'],'missed');self.assertEqual(self.calls,[])
 def test_budget_exhaustion_never_sends(self):
  with closing(sqlite3.connect(self.p/'experiment-budget.db')) as d:d.execute("insert into api_attempts values('spent',242000,3,NULL)");d.commit()
  self.assertEqual(self.tick()['reason'],'budget_exhausted');self.assertEqual(self.calls,[])
 def test_changed_or_unavailable_source_never_sends(self):
  self.snap['symbol']='ETHUSDT';self.assertEqual(self.tick()['status'],'source_unavailable_or_stale');self.assertEqual(self.calls,[])
 def test_startup_publishes_waiting_status_without_fetch_or_send(self):
  self.now=self.b-PERIOD+120001
  self.assertEqual(self.tick(fetch=lambda:self.fail('no source'),secret=lambda:self.fail('no credential'))['status'],'waiting')
  d=json.loads(self.public.read_text());self.assertTrue(d['diagnostic_only']);self.assertIn('ожидание первой границы',d['summary']);self.assertEqual(self.calls,[])
 def test_concurrent_lock_is_rejected(self):
  from research.adaptive_server_once import exclusive
  with exclusive(self.p):
   with self.assertRaisesRegex(ValueError,'already_running'):self.tick()
 def test_stale_after_database_wait_never_sends(self):
  times=iter((self.now,self.now,self.now+200000))
  with self.assertRaisesRegex(ValueError,'stale_after_lock'):self.tick(clock=lambda:next(times))
  self.assertEqual(self.calls,[])
 def test_saved_response_recovers_locally(self):
  def faulty(db,rid,req,clock):
   def fail(stage):
    if stage=='before_usage':raise RuntimeError('local failure')
   return execute_reserved(db,rid,req,transport=self.fake,clock=clock,fault=fail)
  self.assertEqual(self.tick(send=faulty)['status'],'completed');self.assertEqual(len(self.calls),1)
 def test_restart_after_durable_response_recovers_without_network(self):
  def killed(db,rid,req,clock):
   def stop(stage):
    if stage=='after_response_saved':raise SystemExit('simulated crash')
   return execute_reserved(db,rid,req,transport=self.fake,clock=clock,fault=stop)
  with self.assertRaises(SystemExit):self.tick(send=killed)
  self.assertEqual(len(self.calls),1);self.assertEqual(self.tick(fetch=lambda:self.fail('no fetch'),secret=lambda:self.fail('no credential'))['status'],'recovered_locally');self.assertEqual(len(self.calls),1)
  self.assertIn('baseline того же среза',json.loads(self.public.read_text())['summary'])
 def test_publication_failure_retries_locally_without_paid_call(self):
  original=r.publish;calls=[]
  def fail_once(*args,**kwargs):
   calls.append(1)
   if len(calls)==1:raise OSError('local error')
   return original(*args,**kwargs)
  with patch.object(r,'publish',side_effect=fail_once):
   with self.assertRaises(OSError):self.tick()
   self.assertEqual(self.tick(fetch=lambda:self.fail('no fetch'),secret=lambda:self.fail('no credential'))['status'],'completed')
  self.assertEqual(len(self.calls),1);self.assertEqual(len(calls),2)
 def test_downtime_marks_missed_without_backdated_requests(self):
  self.assertEqual(self.tick()['status'],'completed');self.b+=3*PERIOD;self.now=self.b+120001;self.make_snapshot();self.assertEqual(self.tick()['status'],'completed')
  with closing(sqlite3.connect(self.p/'experiment-budget.db')) as db:self.assertEqual(db.execute("select count(*) from regular_windows where state='missed'").fetchone()[0],2)
  self.assertEqual(len(self.calls),2)
 def test_expired_window_publishes_missed_status_without_request(self):
  self.now=self.b+300001
  self.assertEqual(self.tick()['status'],'missed')
  self.assertEqual(self.calls,[])
  self.assertIn('окно пропущено',json.loads(self.public.read_text())['summary'])
 def test_daily_limit_blocks_before_send(self):
  from research.adaptive_budget_resilience import migrate
  migrate(self.p)
  with closing(sqlite3.connect(self.p/'experiment-budget.db')) as db:
   r.schema(db);base=self.b//r.DAY*r.DAY
   for k in range(6):db.execute('insert into api_attempts values(?,?,?,NULL)',('test:'+str(k),1,base/1000+k))
   db.commit()
  self.assertEqual(self.tick()['reason'],'daily_limit');self.assertEqual(self.calls,[])
 def test_horizons_are_recorded_only_at_exact_future_boundary(self):
  origin=self.b
  for n in range(7):
   if n:self.b+=PERIOD;self.now=self.b+120001;self.make_snapshot()
   self.assertEqual(self.tick()['status'],'completed')
  with closing(sqlite3.connect(self.p/'experiment-budget.db')) as db:
   rows=db.execute('select hours,close_ms,return_pct from regular_horizons where boundary_ms=? order by hours',(origin,)).fetchall()
   self.assertEqual([(x[0],x[1]) for x in rows],[(4,origin+PERIOD),(24,origin+6*PERIOD)])
  self.assertEqual(len(self.calls),7)
 def test_paused_diagnostic_can_be_published_without_model_label(self):
  from research.adaptive_server_publish import publish
  from trading_bot.ai_view import render
  def fail(*a,**k):raise TimeoutError()
  self.tick(send=fail);target=self.p/'telegram'/'report.json';target.parent.mkdir()
  self.assertTrue(publish(self.public,target,now=self.now/1000,gid=__import__('os').getgid()))
  text=render(target,self.now/1000);self.assertIn('Состояние запроса:',text);self.assertNotIn('Вывод модели:',text);self.assertIn('неопределённая попытка',text)
 def test_ai_refresh_button_only_reads_and_is_authorized(self):
  from unittest.mock import patch
  from trading_bot.unified_queue import UnifiedQueue
  q=UnifiedQueue(self.p/'queue.db')
  try:
   with patch('trading_bot.ai_view.render',return_value='read-only'),patch('research.adaptive_ai_executor._send',side_effect=AssertionError('paid call forbidden')):
    q.receive(1,False,'/ai',now=100);self.assertEqual(q.db.execute('select count(*) from unified_outbox').fetchone()[0],0)
    q.receive(2,True,'/ai',now=100);token=q.db.execute("select key from unified_state where key like 'inline-action:%' limit 1").fetchone()[0].split('inline-action:')[1]
    q.receive(3,True,'',callback_data=token,now=101);q.receive(3,True,'',callback_data=token,now=101)
    self.assertEqual(q.db.execute('select count(*) from unified_jobs').fetchone()[0],0);self.assertEqual(q.db.execute('select count(*) from unified_outbox').fetchone()[0],2)
  finally:q.close()
