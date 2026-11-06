import hashlib,json,os,sqlite3,subprocess,sys,tempfile,threading,unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from tests.test_runtime import symbol_info
from trading_bot.runtime_store import RuntimeStore
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.unified_queue import UnifiedQueue
from trading_bot.app import Application
from trading_bot.runtime_exchange import PaperExchange
from research.adaptive_atomic_spot import enter,exit_on_stop
from research.adaptive_route_state import RouteState
from research.adaptive_spot_worker import process_entry,process_stop
from research.adaptive_sandbox_backup import backup,verify
from research.adaptive_route_policy import decision_digest,candle_digest
from research.adaptive_router_status import status
from research.adaptive_decision_queue import record as record_decision
from research.adaptive_decision_queue import enqueue
from research.adaptive_protection_monitor import observe
from research.adaptive_manual_reconcile import reconcile_spot
from trading_bot.unified_receiver import MarketReceiver
from trading_bot.unified_worker import process_market

class AtomicSpotTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  a=RuntimeStore(self.root/'spot',{'mode':'paper','symbol':'BTCUSDT'});a.close();b=FuturesStore(self.root/'future');b.close()
  self.fin=self.root/'financial';self.qpath=self.root/'queue';consolidate_snapshots(self.root/'spot',self.root/'future',self.fin,captured_at=1)
  sh=SharedPaperStore(self.fin);CrossPaperBook(sh);MarginPaperBook(sh.ledger);sh.close()
  self.q=UnifiedQueue(self.qpath);self.addCleanup(self.q.close)
  with self.q.transaction():
   self.q.db.execute("INSERT INTO unified_updates VALUES(1,'fixture','/research_adaptive_entry','spot')")
   self.q.db.execute("INSERT INTO unified_jobs(update_id,market,command,payload) VALUES(1,'spot','/research_adaptive_entry',?)",(json.dumps({'action_id':'entry'}),))
  self.store=RuntimeStore(self.fin,{'mode':'paper','symbol':'BTCUSDT'},shared=True);self.addCleanup(self.store.close)
  Application(self.store,PaperExchange(),None);self.store.command(2,True,'/resume')
  bar=900000;self.bars=[(i*bar,100.,101.,99.,100.) for i in range(196)];self.bars[-1]=(195*bar,100.,102.,99.,101.);self.now=197*bar
  self.decision={'mode':'TREND_UP','snapshot_id':'s','data_ms':self.now-1000,'expires_ms':self.now+10000,'reasons':['fixture'],'uncertainties':['fixture']}
  self.bind(self.decision)
 def bind(self,decision,source='AI',bars=None,skip_bar=None):
  bars=self.bars if bars is None else bars;skip_bar=(196*900000,100.,101.,99.,100.) if skip_bar is None else skip_bar
  latest={'sha256':decision_digest(decision),'symbol':'BTCUSDT','version':'regular-regime-horizon-v2','source':source,'mode':decision['mode'],'snapshot_id':decision['snapshot_id'],'data_ms':decision['data_ms'],'expires_ms':decision['expires_ms']}
  with self.q.transaction():
   self.q.db.execute('UPDATE unified_jobs SET payload=? WHERE id=1',(json.dumps({'action_id':'entry','decision_sha256':decision_digest(decision),'candles_sha256':candle_digest(bars,skip_bar)}),))
   self.q.db.execute("INSERT OR REPLACE INTO unified_state VALUES('adaptive:latest',?)",(json.dumps(latest),))
 def call(self,**changes):
  args={'job_id':1,'action_id':'entry','decision':self.decision,'snapshot_id':'s','data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':symbol_info(),'filters_ms':self.now,'decision_source':'AI','clock':lambda:self.now};args.update(changes)
  return enter(self.store,self.qpath,**args)
 def test_real_shared_fill_and_reservation(self):
  result=self.call();self.assertEqual(result['status'],'filled');self.assertGreater(float(self.store.get('position')['quantity']),0)
  self.assertEqual(self.store.db.execute('select count(*) from runtime_orders').fetchone()[0],1)
  self.assertTrue(self.store.shared.ledger.reconcile()['balanced'])
  self.assertEqual(self.call()['status'],'filled_existing')
 def test_decision_producer_enqueues_immutable_input_and_replays(self):
  with self.q.transaction():self.q.db.execute('DELETE FROM unified_jobs');self.q.db.execute('DELETE FROM unified_updates')
  skip=(196*900000,100.,101.,99.,100.)
  params={'source':'AI','symbol':'BTCUSDT','version':'regular-regime-horizon-v2','now_ms':self.now,'bars':self.bars,'skip_bar':skip}
  first=enqueue(self.q,self.decision,**params);second=enqueue(self.q,self.decision,**params)
  self.assertEqual(first['job_id'],second['job_id']);self.assertTrue(second['replayed'])
  saved=json.loads(self.q.db.execute('SELECT payload FROM unified_jobs WHERE id=?',(first['job_id'],)).fetchone()[0])
  saved_decision=json.loads(self.q.db.execute('SELECT value FROM unified_state WHERE key=?',('adaptive:decision:'+saved['decision_sha256'],)).fetchone()[0])['decision']
  state=RouteState(self.root/'route',clock=lambda:self.now)
  try:
   signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'}
   result=process_entry(self.q,self.store,state,decision=saved_decision,signal=signal,data_ms=self.now-1000,decision_version='regular-regime-horizon-v2',bars=saved['bars'],skip_bar=saved['skip_bar'],quote='100',quote_ms=self.now,filters=symbol_info(),filters_ms=self.now,clock=lambda:self.now)
   self.assertEqual(result['status'],'OPEN');self.assertEqual(self.store.db.execute('SELECT count(*) FROM runtime_fills').fetchone()[0],1)
  finally:state.close()
 def test_stale_after_lock_does_not_prepare(self):
  held=threading.Event();release=threading.Event()
  def lock():
   d=sqlite3.connect(self.fin,isolation_level=None);d.execute('BEGIN IMMEDIATE');held.set();release.wait(3);d.rollback();d.close()
  thread=threading.Thread(target=lock);thread.start();self.assertTrue(held.wait(2));timer=threading.Timer(.1,release.set);timer.start()
  try:
   with self.assertRaisesRegex(ValueError,'stale_quote'):self.call(clock=lambda:self.now+(6000 if release.is_set() else 0))
  finally:release.set();timer.cancel();thread.join()
  self.assertEqual(self.store.db.execute('select count(*) from runtime_orders').fetchone()[0],0)
 def test_queue_decision_cannot_be_substituted(self):
  with self.assertRaisesRegex(ValueError,'queue_identity_mismatch'):self.call(decision=dict(self.decision,reasons=['different']))
  self.assertEqual(self.store.db.execute('select count(*) from runtime_orders').fetchone()[0],0)
 def test_isolated_short_blocks_conflicting_spot_entry(self):
  f=FuturesStore(self.fin,shared=True)
  try:
   f.command(1,True,'/resume');self.assertTrue(f.open_position('SHORT',Decimal('100'),1000,'other'))
   with self.assertRaisesRegex(ValueError,'position_exists'):self.call()
   self.assertEqual(self.store.db.execute('select count(*) from runtime_orders').fetchone()[0],0)
  finally:f.close()
 def test_quote_expiring_after_reservation_never_submits(self):
  times={'now':self.now}
  def fault(stage):
   if stage=='after_prepare':times['now']+=6000
  with self.assertRaisesRegex(ValueError,'stale_quote'):self.call(clock=lambda:times['now'],fault=fault)
  row=self.store.db.execute('select phase from runtime_orders').fetchone()
  self.assertEqual(row['phase'],'PREPARED');self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],0)
  self.assertEqual(self.call()['status'],'reconciliation_required')
 def test_new_no_trade_between_reserve_and_claim_prevents_submission(self):
  newer=dict(self.decision,mode='NO_TRADE',snapshot_id='new',data_ms=self.now,expires_ms=self.now+10000)
  def update(stage):
   if stage=='after_prepare':record_decision(self.q,newer,source='AI',symbol='BTCUSDT',version='regular-regime-horizon-v2',now_ms=self.now)
  with self.assertRaisesRegex(ValueError,'decision_superseded'):self.call(fault=update)
  self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],0)
  self.assertEqual(self.store.db.execute('select phase from runtime_orders').fetchone()[0],'PREPARED')
 def test_conflicting_same_time_decisions_block_entries_durably(self):
  conflict=dict(self.decision,mode='NO_TRADE')
  with self.assertRaisesRegex(ValueError,'decision_conflict_requires_review'):
   record_decision(self.q,conflict,source='AI',symbol='BTCUSDT',version='regular-regime-horizon-v2',now_ms=self.now)
  self.assertIsNotNone(self.q.db.execute("SELECT value FROM unified_state WHERE key='adaptive:decision_conflict'").fetchone())
  with self.assertRaisesRegex(ValueError,'decision_conflict_requires_review'):self.call()
  self.assertEqual(self.store.db.execute('select count(*) from runtime_orders').fetchone()[0],0)
 def test_decision_expiring_after_financial_lock_wait_never_prepares(self):
  short_decision=dict(self.decision,expires_ms=self.now+1000);self.bind(short_decision)
  held=threading.Event();release=threading.Event()
  def lock():
   d=sqlite3.connect(self.fin,isolation_level=None);d.execute('BEGIN IMMEDIATE');held.set();release.wait(3);d.rollback();d.close()
  thread=threading.Thread(target=lock);thread.start();self.assertTrue(held.wait(2));timer=threading.Timer(.1,release.set);timer.start()
  try:
   with self.assertRaisesRegex(ValueError,'invalid_or_expired_decision'):
    self.call(decision=short_decision,clock=lambda:self.now+(2000 if release.is_set() else 0))
  finally:release.set();timer.cancel();thread.join()
  self.assertEqual(self.store.db.execute('select count(*) from runtime_orders').fetchone()[0],0)
 def test_filters_expiring_after_financial_lock_wait_never_prepares(self):
  held=threading.Event();release=threading.Event()
  def lock():
   d=sqlite3.connect(self.fin,isolation_level=None);d.execute('BEGIN IMMEDIATE');held.set();release.wait(3);d.rollback();d.close()
  thread=threading.Thread(target=lock);thread.start();self.assertTrue(held.wait(2));timer=threading.Timer(.1,release.set);timer.start()
  try:
   with self.assertRaisesRegex(ValueError,'stale_filters'):
    self.call(filters_ms=self.now-119000,clock=lambda:self.now+(2000 if release.is_set() else 0))
  finally:release.set();timer.cancel();thread.join()
  self.assertEqual(self.store.db.execute('select count(*) from runtime_orders').fetchone()[0],0)
 def test_crash_after_fill_replay_does_not_duplicate(self):
  def fault(stage):
   if stage=='after_fill':raise RuntimeError('crash')
  with self.assertRaisesRegex(RuntimeError,'crash'):self.call(fault=fault)
  self.assertEqual(self.call()['status'],'filled_existing')
  self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],1)
 def test_two_workers_cannot_prepare_or_fill_twice(self):
  barrier=threading.Barrier(2);results=[];errors=[]
  def worker():
   store=RuntimeStore(self.fin,{'mode':'paper','symbol':'BTCUSDT'},shared=True)
   try:
    barrier.wait()
    args={'job_id':1,'action_id':'entry','decision':self.decision,'snapshot_id':'s','data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':symbol_info(),'filters_ms':self.now,'decision_source':'AI','clock':lambda:self.now}
    try:results.append(enter(store,self.qpath,**args)['status'])
    except Exception as exc:errors.append(str(exc))
   finally:store.close()
  threads=[threading.Thread(target=worker) for _ in range(2)]
  for t in threads:t.start()
  for t in threads:t.join(10);self.assertFalse(t.is_alive())
  self.assertFalse(errors);self.assertEqual(results.count('filled'),1)
  self.assertEqual(self.store.db.execute('select count(*) from runtime_orders').fetchone()[0],1)
  self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],1)
 def test_stop_exits_while_paused_without_ai_and_replay_is_safe(self):
  state=RouteState(self.root/'route',clock=lambda:self.now)
  try:
   signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'}
   state.begin_entry('entry',self.decision,signal,snapshot_id='s',data_ms=self.now-1000,symbol='BTCUSDT',decision_version='regular-regime-horizon-v2',quote_ms=self.now,expiry_ms=self.now+5000,eligible=True,research_sandbox=True)
   self.call();pos=state.confirm_open('entry',self.fin)['position_id'];self.q.acknowledge(1,'filled',notify=False)
   self.store.command(3,True,'/pause')
   with self.q.transaction():
    self.q.db.execute("INSERT INTO unified_updates VALUES(2,'exit','/research_adaptive_exit','spot')")
    self.q.db.execute("INSERT INTO unified_jobs(update_id,market,command,payload) VALUES(2,'spot','/research_adaptive_exit',?)",(json.dumps({'action_id':'stop'}),))
   args={'route_state':state,'job_id':2,'action_id':'stop','position_id':pos,'quote':'97','quote_ms':self.now,'clock':lambda:self.now}
   state.begin_exit('entry','stop')
   self.assertEqual(exit_on_stop(self.store,self.qpath,**args)['status'],'filled')
   self.assertEqual(float(self.store.get('position')['quantity']),0);self.assertTrue(self.store.get('paused'))
   self.assertEqual(exit_on_stop(self.store,self.qpath,**args)['status'],'filled_existing')
   self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],2)
   self.assertEqual(state.confirm_spot_closed('entry','stop',self.fin)['phase'],'IDLE')
   self.assertTrue(state.confirm_spot_closed('entry','stop',self.fin)['replayed'])
   with self.assertRaisesRegex(ValueError,'identity_conflict'):state.confirm_spot_closed('entry','other',self.fin)
  finally:state.close()
 def test_worker_queue_fill_pause_stop_and_lost_ack(self):
  state=RouteState(self.root/'route',clock=lambda:self.now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'}
  args={'decision':self.decision,'signal':signal,'data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':symbol_info(),'filters_ms':self.now,'clock':lambda:self.now}
  def crash(stage):
   if stage=='before_queue_ack':raise RuntimeError('lost ack')
  try:
   with self.assertRaisesRegex(RuntimeError,'lost ack'):process_entry(self.q,self.store,state,**args,fault=crash)
   self.assertEqual(process_entry(self.q,self.store,state,**args)['status'],'OPEN')
   self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],1)
   before=[hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root/'route',self.qpath)]
   view=status(self.root/'route',self.qpath,now_ms=self.now)
   self.assertEqual((view['source'],view['mode'],view['phase'],view['protection']),('AI','TREND_UP','OPEN','not_evaluated'))
   self.assertEqual(before,[hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root/'route',self.qpath)])
   newer=dict(self.decision,mode='NO_TRADE',snapshot_id='new',data_ms=self.now,expires_ms=self.now+10000)
   record_decision(self.q,newer,source='AI',symbol='BTCUSDT',version='regular-regime-horizon-v2',now_ms=self.now)
   view=status(self.root/'route',self.qpath,now_ms=self.now)
   self.assertEqual((view['mode'],view['entry_mode'],view['strategy'],view['phase']),('NO_TRADE','TREND_UP','trend1','OPEN'))
   self.store.command(3,True,'/pause')
   with self.q.transaction():
    self.q.db.execute("INSERT INTO unified_updates VALUES(2,'exit','/research_adaptive_exit','spot')")
    self.q.db.execute("INSERT INTO unified_jobs(update_id,market,command,payload) VALUES(2,'spot','/research_adaptive_exit',?)",(json.dumps({'action_id':'stop','entry_action':'entry'}),))
   exitargs={'quote':'97','quote_ms':self.now,'clock':lambda:self.now}
   with self.assertRaisesRegex(RuntimeError,'lost ack'):process_stop(self.q,self.store,state,**exitargs,fault=crash)
   self.assertEqual(process_stop(self.q,self.store,state,**exitargs)['status'],'IDLE')
   self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],2)
   self.assertTrue(self.store.get('paused'));self.assertTrue(self.store.shared.ledger.reconcile()['balanced'])
  finally:state.close()
 def test_monitor_schedules_one_protective_exit_without_ai(self):
  state=RouteState(self.root/'route',clock=lambda:self.now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'}
  args={'decision':self.decision,'signal':signal,'data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':symbol_info(),'filters_ms':self.now,'clock':lambda:self.now}
  try:
   process_entry(self.q,self.store,state,**args);self.store.command(3,True,'/pause')
   stale=observe(self.q,self.store,state,quote='97',quote_ms=self.now-6000,clock=lambda:self.now)
   self.assertEqual((stale['state'],stale['reason']),('unavailable','price_unavailable_or_stale'));self.assertFalse(self.q.pending('spot'))
   first=observe(self.q,self.store,state,quote='97',quote_ms=self.now,bars=[],skip_bar=(1,2,3,4,5),clock=lambda:self.now)
   self.assertEqual((first['state'],first['channel']),('partial','unavailable'))
   again=observe(self.q,self.store,state,quote='97',quote_ms=self.now,clock=lambda:self.now)
   self.assertEqual(first['trigger'],'stop');self.assertEqual(first['job_id'],again['job_id']);self.assertTrue(again['replayed'])
   def fail(stage):
    if stage=='after_exit_route_commit':raise RuntimeError('exit intent crash')
   with self.assertRaisesRegex(RuntimeError,'exit intent crash'):process_stop(self.q,self.store,state,quote='97',quote_ms=self.now,clock=lambda:self.now,fault=fail)
   pending=observe(self.q,self.store,state,quote='97',quote_ms=self.now,clock=lambda:self.now)
   self.assertTrue(pending['exit_pending']);self.assertEqual(pending['job_id'],first['job_id']);self.assertFalse(pending['reconciliation_required'])
   self.assertEqual(process_stop(self.q,self.store,state,quote='97',quote_ms=self.now,clock=lambda:self.now)['status'],'IDLE')
   self.assertTrue(self.store.get('paused'));self.assertTrue(self.store.shared.ledger.reconcile()['balanced'])
  finally:state.close()
 def test_protection_runs_before_lost_entry_ack_and_later_reconciles(self):
  state=RouteState(self.root/'route',clock=lambda:self.now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'}
  args={'decision':self.decision,'signal':signal,'data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':symbol_info(),'filters_ms':self.now,'clock':lambda:self.now}
  def fail(stage):
   if stage=='before_queue_ack':raise RuntimeError('lost entry ack')
  try:
   with self.assertRaisesRegex(RuntimeError,'lost entry ack'):process_entry(self.q,self.store,state,**args,fault=fail)
   self.assertEqual(observe(self.q,self.store,state,quote='97',quote_ms=self.now,clock=lambda:self.now)['trigger'],'stop')
   self.assertEqual(process_stop(self.q,self.store,state,quote='97',quote_ms=self.now,clock=lambda:self.now)['status'],'IDLE')
   self.assertEqual(process_entry(self.q,self.store,state,**args)['status'],'IDLE')
   self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],2);self.assertFalse(self.q.pending('spot'))
  finally:state.close()
 def test_trend_max_hold_exits_without_a_live_decision(self):
  state=RouteState(self.root/'route',clock=lambda:self.now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'}
  args={'decision':self.decision,'signal':signal,'data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':symbol_info(),'filters_ms':self.now,'clock':lambda:self.now}
  try:
   process_entry(self.q,self.store,state,**args);later=self.now+604800000
   self.assertEqual(observe(self.q,self.store,state,quote='100',quote_ms=later,clock=lambda:later)['trigger'],'time')
   self.assertEqual(process_stop(self.q,self.store,state,quote='100',quote_ms=later,clock=lambda:later)['status'],'IDLE')
   self.assertEqual(self.store.db.execute("SELECT reason FROM runtime_orders WHERE side='SELL'").fetchone()[0],'ADAPTIVE_TIME')
  finally:state.close()
 def test_conflicting_decision_never_disables_open_position_stop(self):
  state=RouteState(self.root/'route',clock=lambda:self.now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'}
  args={'decision':self.decision,'signal':signal,'data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':symbol_info(),'filters_ms':self.now,'clock':lambda:self.now}
  try:
   process_entry(self.q,self.store,state,**args)
   with self.assertRaisesRegex(ValueError,'decision_conflict_requires_review'):
    record_decision(self.q,dict(self.decision,mode='NO_TRADE'),source='AI',symbol='BTCUSDT',version='regular-regime-horizon-v2',now_ms=self.now)
   self.assertEqual(observe(self.q,self.store,state,quote='97',quote_ms=self.now,clock=lambda:self.now)['trigger'],'stop')
   self.assertEqual(process_stop(self.q,self.store,state,quote='97',quote_ms=self.now,clock=lambda:self.now)['status'],'IDLE')
  finally:state.close()
 def test_confirmed_manual_close_after_mode_change_reconciles_route(self):
  state=RouteState(self.root/'route',clock=lambda:self.now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'}
  args={'decision':self.decision,'signal':signal,'data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':symbol_info(),'filters_ms':self.now,'clock':lambda:self.now}
  class Reader:
   def get(self,path,params=None):
    if path.endswith('/ticker/price'):return {'symbol':'BTCUSDT','price':'100'}
    if path.endswith('/exchangeInfo'):return {'symbols':[symbol_info()]}
    raise ValueError('unexpected fixture call')
  receiver=MarketReceiver(self.fin,'spot')
  try:
   process_entry(self.q,self.store,state,**args);position=self.store.get('position')
   newer=dict(self.decision,mode='NO_TRADE',snapshot_id='new',data_ms=self.now,expires_ms=self.now+10000)
   record_decision(self.q,newer,source='AI',symbol='BTCUSDT',version='regular-regime-horizon-v2',now_ms=self.now)
   self.q.receive(2,True,'/close',positions={'spot':position});process_market(self.q,receiver,self.store,'spot')
   token=self.q.db.execute("SELECT id FROM unified_proposals WHERE status='OPEN'").fetchone()[0]
   self.q.receive(3,True,'/confirm '+token);process_market(self.q,receiver,self.store,'spot')
   app=Application(self.store,PaperExchange(),Reader());self.assertTrue(app.protective_close())
   self.assertEqual(reconcile_spot(self.q,self.store,state)['phase'],'IDLE')
   self.assertTrue(self.store.get('paused'));self.assertEqual(float(self.store.get('position')['quantity']),0)
   self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],2)
  finally:receiver.close();state.close()
 def test_crash_after_claim_recovers_paper_without_second_submission(self):
  state=RouteState(self.root/'route',clock=lambda:self.now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'}
  args={'decision':self.decision,'signal':signal,'data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':symbol_info(),'filters_ms':self.now,'clock':lambda:self.now}
  def crash(stage):
   if stage=='after_claim':raise RuntimeError('crash after durable claim')
  try:
   with self.assertRaisesRegex(RuntimeError,'durable claim'):process_entry(self.q,self.store,state,**args,fault=crash)
   self.assertEqual(process_entry(self.q,self.store,state,**args)['status'],'reconciliation_required')
   self.assertEqual(state.status()['phase'],'RECONCILE')
   app=Application(self.store,PaperExchange(),None);app.recover()
   self.assertEqual(process_entry(self.q,self.store,state,**args)['status'],'OPEN')
   self.assertTrue(self.store.get('paused'));self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],1)
  finally:state.close()
 def test_restart_expires_unsent_preparation_and_releases_route(self):
  state=RouteState(self.root/'route',clock=lambda:self.now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'}
  args={'decision':self.decision,'signal':signal,'data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':symbol_info(),'filters_ms':self.now,'clock':lambda:self.now}
  def crash(stage):
   if stage=='after_prepare':raise RuntimeError('lost before submission')
  try:
   with self.assertRaisesRegex(RuntimeError,'before submission'):process_entry(self.q,self.store,state,**args,fault=crash)
   self.assertEqual(process_entry(self.q,self.store,state,**args)['status'],'reconciliation_required');self.assertEqual(state.status()['phase'],'RECONCILE')
   Application(self.store,PaperExchange(),None).recover()
   self.assertEqual(process_entry(self.q,self.store,state,**args)['status'],'CANCELED')
   self.assertEqual(state.status()['phase'],'IDLE');self.assertTrue(self.store.get('paused'));self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],0)
  finally:state.close()
 def test_process_death_after_claim_recovers_without_duplicate_fill(self):
  if os.name!='posix':self.skipTest('POSIX process semantics required')
  child=r'''
import json,os,sys
from pathlib import Path
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue
from research.adaptive_route_state import RouteState
from research.adaptive_spot_worker import process_entry
from tests.test_runtime import symbol_info
p=Path(sys.argv[1]);now=197*900000
s=RuntimeStore(p/'financial',{'mode':'paper','symbol':'BTCUSDT'},shared=True);q=UnifiedQueue(p/'queue');state=RouteState(p/'route',clock=lambda:now)
decision={'mode':'TREND_UP','snapshot_id':'s','data_ms':now-1000,'expires_ms':now+10000,'reasons':['fixture'],'uncertainties':['fixture']}
signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'}
bars=[(i*900000,100.,101.,99.,100.) for i in range(196)];bars[-1]=(195*900000,100.,102.,99.,101.)
def fail(stage):
 if stage=='after_claim':os._exit(73)
process_entry(q,s,state,decision=decision,signal=signal,data_ms=now-1000,decision_version='regular-regime-horizon-v2',bars=bars,skip_bar=(196*900000,100.,101.,99.,100.),quote='100',quote_ms=now,filters=symbol_info(),filters_ms=now,clock=lambda:now,fault=fail)
'''
  result=subprocess.run([sys.executable,'-B','-c',child,str(self.root)],timeout=20);self.assertEqual(result.returncode,73)
  state=RouteState(self.root/'route',clock=lambda:self.now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'}
  args={'decision':self.decision,'signal':signal,'data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':symbol_info(),'filters_ms':self.now,'clock':lambda:self.now}
  try:
   self.assertEqual(process_entry(self.q,self.store,state,**args)['status'],'reconciliation_required')
   app=Application(self.store,PaperExchange(),None);app.recover()
   self.assertEqual(process_entry(self.q,self.store,state,**args)['status'],'OPEN')
   self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],1);self.assertTrue(self.store.get('paused'))
  finally:state.close()
 def test_process_death_after_exit_fill_recovers_without_double_sell(self):
  if os.name!='posix':self.skipTest('POSIX process semantics required')
  state=RouteState(self.root/'route',clock=lambda:self.now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'}
  args={'decision':self.decision,'signal':signal,'data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':symbol_info(),'filters_ms':self.now,'clock':lambda:self.now}
  try:
   process_entry(self.q,self.store,state,**args)
   self.assertEqual(observe(self.q,self.store,state,quote='97',quote_ms=self.now,clock=lambda:self.now)['trigger'],'stop')
   child=r'''
import os,sys
from pathlib import Path
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue
from research.adaptive_route_state import RouteState
from research.adaptive_spot_worker import process_stop
p=Path(sys.argv[1]);now=197*900000
s=RuntimeStore(p/'financial',{'mode':'paper','symbol':'BTCUSDT'},shared=True);q=UnifiedQueue(p/'queue');state=RouteState(p/'route',clock=lambda:now)
def fail(stage):
 if stage=='after_fill':os._exit(73)
process_stop(q,s,state,quote='97',quote_ms=now,clock=lambda:now,fault=fail)
'''
   self.assertEqual(subprocess.run([sys.executable,'-B','-c',child,str(self.root)],timeout=20).returncode,73)
   self.assertEqual(state.status()['phase'],'EXIT_PENDING')
   self.assertEqual(process_stop(self.q,self.store,state,quote='97',quote_ms=self.now,clock=lambda:self.now)['status'],'IDLE')
   self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],2);self.assertTrue(self.store.shared.ledger.reconcile()['balanced'])
  finally:state.close()
 def test_range_signal_entry_and_target_preserve_rule_after_mode_change(self):
  bar=900000;bars=[(i*bar,99.,100.,98.,99.) for i in range(96)]+[(96*bar,98.5,99.,98.1,98.2)]
  now=98*bar;decision={'mode':'RANGE','snapshot_id':'range','data_ms':now-1000,'expires_ms':now+10000,'reasons':['fixture'],'uncertainties':['fixture']}
  self.bind(decision,bars=bars,skip_bar=(97*bar,98.3,99.9,98.1,98.4))
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'range','strategy':'range','direction':'LONG'}
  state=RouteState(self.root/'route',clock=lambda:now)
  args={'decision':decision,'signal':signal,'data_ms':now-1000,'decision_version':'regular-regime-horizon-v2','bars':bars,'skip_bar':(97*bar,98.3,99.9,98.1,98.4),'quote':'98.3','quote_ms':now,'filters':symbol_info(),'filters_ms':now,'clock':lambda:now}
  try:
   self.assertEqual(process_entry(self.q,self.store,state,**args)['status'],'OPEN');self.assertEqual(state.status()['strategy'],'range')
   # An incompatible decision cannot acquire a second slot while the position is open.
   with self.assertRaisesRegex(ValueError,'exposure_or_pending'):
    state.begin_entry('other',dict(decision,mode='TREND_DOWN'),dict(signal,strategy='trend1',direction='SHORT'),snapshot_id='range',data_ms=now-1000,symbol='BTCUSDT',decision_version='regular-regime-horizon-v2',quote_ms=now,expiry_ms=now+5000,eligible=True,research_sandbox=True)
   with self.q.transaction():
    self.q.db.execute("INSERT INTO unified_updates VALUES(2,'exit','/research_adaptive_exit','spot')")
    self.q.db.execute("INSERT INTO unified_jobs(update_id,market,command,payload) VALUES(2,'spot','/research_adaptive_exit',?)",(json.dumps({'action_id':'target','entry_action':'entry'}),))
   self.assertEqual(process_stop(self.q,self.store,state,quote='99',quote_ms=now,clock=lambda:now)['status'],'IDLE')
   self.assertEqual(self.store.db.execute("SELECT reason FROM runtime_orders WHERE business_key='target'").fetchone()[0],'ADAPTIVE_TARGET')
  finally:state.close()
 def test_range_intrabar_stop_precedes_target_and_uses_conservative_fill(self):
  bar=900000;bars=[(i*bar,99.,100.,98.,99.) for i in range(96)]+[(96*bar,98.5,99.,98.1,98.2)]
  now=98*bar;decision={'mode':'RANGE','snapshot_id':'range','data_ms':now-1000,'expires_ms':now+10000,'reasons':['fixture'],'uncertainties':['fixture']}
  self.bind(decision,bars=bars,skip_bar=(97*bar,98.3,99.9,98.1,98.4));state=RouteState(self.root/'route',clock=lambda:now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'range','strategy':'range','direction':'LONG'}
  args={'decision':decision,'signal':signal,'data_ms':now-1000,'decision_version':'regular-regime-horizon-v2','bars':bars,'skip_bar':(97*bar,98.3,99.9,98.1,98.4),'quote':'98.3','quote_ms':now,'filters':symbol_info(),'filters_ms':now,'clock':lambda:now}
  try:
   process_entry(self.q,self.store,state,**args);later=now+bar;risk=(now,98.3,100.,96.,99.5)
   self.assertEqual(observe(self.q,self.store,state,quote='99.5',quote_ms=later,risk_bar=risk,clock=lambda:later)['trigger'],'stop')
   self.assertEqual(process_stop(self.q,self.store,state,quote='99.5',quote_ms=later,clock=lambda:later)['status'],'IDLE')
   sell=self.store.db.execute("SELECT reason,price FROM runtime_orders WHERE side='SELL'").fetchone()
   self.assertEqual(sell['reason'],'ADAPTIVE_STOP');self.assertLess(Decimal(sell['price']),Decimal('98.3'))
  finally:state.close()
 def test_range_timeout_closes_after_24_hours(self):
  bar=900000;bars=[(i*bar,99.,100.,98.,99.) for i in range(96)]+[(96*bar,98.5,99.,98.1,98.2)]
  now=98*bar;decision={'mode':'RANGE','snapshot_id':'range','data_ms':now-1000,'expires_ms':now+10000,'reasons':['fixture'],'uncertainties':['fixture']}
  self.bind(decision,bars=bars,skip_bar=(97*bar,98.3,99.9,98.1,98.4));state=RouteState(self.root/'route',clock=lambda:now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'range','strategy':'range','direction':'LONG'}
  args={'decision':decision,'signal':signal,'data_ms':now-1000,'decision_version':'regular-regime-horizon-v2','bars':bars,'skip_bar':(97*bar,98.3,99.9,98.1,98.4),'quote':'98.3','quote_ms':now,'filters':symbol_info(),'filters_ms':now,'clock':lambda:now}
  try:
   process_entry(self.q,self.store,state,**args);later=now+86400000
   self.assertEqual(observe(self.q,self.store,state,quote='98.4',quote_ms=later,clock=lambda:later)['trigger'],'time')
   self.assertEqual(process_stop(self.q,self.store,state,quote='98.4',quote_ms=later,clock=lambda:later)['status'],'IDLE')
   self.assertEqual(self.store.db.execute("SELECT reason FROM runtime_orders WHERE side='SELL'").fetchone()[0],'ADAPTIVE_TIME')
  finally:state.close()
 def test_coordinated_backup_restores_pending_ack_without_second_fill(self):
  state=RouteState(self.root/'route',clock=lambda:self.now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'}
  args={'decision':self.decision,'signal':signal,'data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':symbol_info(),'filters_ms':self.now,'clock':lambda:self.now}
  def crash(stage):
   if stage=='before_queue_ack':raise RuntimeError('lost ack')
  try:
   with self.assertRaisesRegex(RuntimeError,'lost ack'):process_entry(self.q,self.store,state,**args,fault=crash)
   obs=self.root/'observation';db=sqlite3.connect(obs);db.execute('create table samples(id integer primary key)');db.commit();db.close()
   bundle=self.root/'backup';backup(self.fin,self.qpath,obs,self.root/'route',bundle);verify(bundle)
   s=RuntimeStore(bundle/'financial.db',{'mode':'paper','symbol':'BTCUSDT'},shared=True);q=UnifiedQueue(bundle/'queue.db');r=RouteState(bundle/'route.db',clock=lambda:self.now)
   try:
    app=Application(s,PaperExchange(),None);app.recover()
    self.assertEqual(process_entry(q,s,r,**args)['status'],'OPEN')
    self.assertTrue(s.get('paused'));self.assertEqual(s.db.execute('select count(*) from runtime_fills').fetchone()[0],1)
    self.assertTrue(s.shared.ledger.reconcile()['balanced'])
   finally:r.close();q.close();s.close()
  finally:state.close()
 def test_trend_reverse_channel_exit_uses_fixed_delay(self):
  state=RouteState(self.root/'route',clock=lambda:self.now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'LONG'}
  args={'decision':self.decision,'signal':signal,'data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':symbol_info(),'filters_ms':self.now,'clock':lambda:self.now}
  try:
   process_entry(self.q,self.store,state,**args)
   bar=900000;bars=[((i+4)*bar,100.,101.,99.,100.) for i in range(196)];bars[-1]=(199*bar,100.,101.,98.,99.);now=201*bar
   self.assertEqual(observe(self.q,self.store,state,quote='100',quote_ms=now,bars=bars,skip_bar=(200*bar,100.,101.,99.,100.),clock=lambda:now)['trigger'],'signal')
   result=process_stop(self.q,self.store,state,quote='100',quote_ms=now,clock=lambda:now)
   self.assertEqual(result['status'],'IDLE');self.assertEqual(self.store.db.execute("SELECT reason FROM runtime_orders WHERE side='SELL'").fetchone()[0],'ADAPTIVE_SIGNAL')
  finally:state.close()
