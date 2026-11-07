import json,os,sqlite3,subprocess,sys,tempfile,threading,time,unittest
from decimal import Decimal
from pathlib import Path
from tests.test_paper_pair_catalog import spot_info
from trading_bot.runtime_store import RuntimeStore
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.unified_queue import UnifiedQueue
from trading_bot.app import Application
from trading_bot.runtime_exchange import PaperExchange
from research.adaptive_atomic_futures import enter_short
from research.adaptive_route_state import RouteState
from research.adaptive_spot_worker import process_short,process_short_stop
from research.adaptive_route_policy import decision_digest,candle_digest
from research.adaptive_protection_monitor import observe
from research.adaptive_sandbox_backup import backup,verify
from research.adaptive_manual_reconcile import reconcile_futures
from research.adaptive_decision_queue import record
from research.adaptive_decision_queue import enqueue
from trading_bot.unified_receiver import MarketReceiver
from trading_bot.unified_worker import process_market

class AtomicFuturesTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);p=Path(self.tmp.name)
  a=RuntimeStore(p/'spot',{'mode':'paper','symbol':'BTCUSDT'});a.close();b=FuturesStore(p/'future',fee_bps='10',slippage_bps='5');b.close()
  self.fin=p/'financial';self.qpath=p/'queue';consolidate_snapshots(p/'spot',p/'future',self.fin,captured_at=1)
  sh=SharedPaperStore(self.fin);CrossPaperBook(sh);MarginPaperBook(sh.ledger);sh.close()
  self.q=UnifiedQueue(self.qpath);self.addCleanup(self.q.close)
  with self.q.transaction():
   self.q.db.execute("INSERT INTO unified_updates VALUES(1,'fixture','/research_adaptive_entry','futures')")
   self.q.db.execute("INSERT INTO unified_jobs(update_id,market,command,payload) VALUES(1,'futures','/research_adaptive_entry',?)",(json.dumps({'action_id':'short'}),))
  self.store=FuturesStore(self.fin,shared=True,fee_bps='10',slippage_bps='5');self.addCleanup(self.store.close);self.store.command(1,True,'/resume')
  bar=900000;self.bars=[(i*bar,100.,101.,99.,100.) for i in range(196)];self.bars[-1]=(195*bar,100.,101.,98.,99.);self.now=197*bar
  self.decision={'mode':'TREND_DOWN','snapshot_id':'s','data_ms':self.now-1000,'expires_ms':self.now+10000,'reasons':['fixture'],'uncertainties':['fixture']}
  self.funding={'symbol':'BTCUSDT','complete':True,'observed_ms':self.now,'next_due_ms':self.now+28800000,'source':'synthetic'}
  with self.q.transaction():
   self.q.db.execute('UPDATE unified_jobs SET payload=? WHERE id=1',(json.dumps({'action_id':'short','decision_sha256':decision_digest(self.decision),'candles_sha256':candle_digest(self.bars,(196*900000,100.,101.,99.,100.)),'funding_sha256':decision_digest(self.funding)}),))
   self.q.db.execute("INSERT INTO unified_state VALUES('adaptive:latest',?)",(json.dumps({'sha256':decision_digest(self.decision),'symbol':'BTCUSDT','version':'regular-regime-horizon-v2','source':'AI','data_ms':self.decision['data_ms'],'expires_ms':self.decision['expires_ms']}),))
  self.filters=spot_info('BTC');self.filters.update(contractType='PERPETUAL',marginAsset='USDT')
 def call(self,**changes):
  args={'job_id':1,'action_id':'short','decision':self.decision,'snapshot_id':'s','data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':self.filters,'filters_ms':self.now,'funding_evidence':self.funding,'decision_source':'AI','clock':lambda:self.now};args.update(changes)
  return enter_short(self.store,self.qpath,**args)
 def test_real_short_fee_funding_and_replay(self):
  self.assertEqual(self.call()['status'],'filled');p=self.store.get('position');self.assertEqual(p['direction'],'SHORT');self.assertEqual(p['leverage'],1)
  self.assertLessEqual(Decimal(p['quantity'])*Decimal(p['entry']),Decimal('10'));self.assertEqual(self.call()['status'],'filled_existing')
  self.store.apply_funding(p['id'],[{'time':self.now+1000,'rate':'.001','mark':'100'}],self.now+1000)
  self.assertGreater(Decimal(self.store.get('position')['funding']),0)
  self.store.close_position(Decimal('98'),self.now+1000,'exit','TEST',p['id'])
  self.assertTrue(self.store.shared.ledger.reconcile()['balanced']);self.assertEqual(self.store.db.execute("select count(*) from futures_events where event='OPEN'").fetchone()[0],1)
 def test_decision_producer_short_job_uses_saved_evidence(self):
  with self.q.transaction():self.q.db.execute('DELETE FROM unified_jobs');self.q.db.execute('DELETE FROM unified_updates')
  skip=(196*900000,100.,101.,99.,100.)
  params={'source':'AI','symbol':'BTCUSDT','version':'regular-regime-horizon-v2','now_ms':self.now,'bars':self.bars,'skip_bar':skip,'funding_evidence':self.funding}
  first=enqueue(self.q,self.decision,**params);self.assertTrue(enqueue(self.q,self.decision,**params)['replayed'])
  saved=json.loads(self.q.db.execute('SELECT payload FROM unified_jobs WHERE id=?',(first['job_id'],)).fetchone()[0]);state=RouteState(self.fin.parent/'route',clock=lambda:self.now)
  try:
   signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'SHORT'}
   result=process_short(self.q,self.store,state,decision=self.decision,signal=signal,data_ms=self.now-1000,decision_version='regular-regime-horizon-v2',bars=saved['bars'],skip_bar=saved['skip_bar'],quote='100',quote_ms=self.now,filters=self.filters,filters_ms=self.now,funding_evidence=saved['funding_evidence'],clock=lambda:self.now)
   self.assertEqual(result['status'],'OPEN');self.assertEqual(self.store.get('position')['direction'],'SHORT')
  finally:state.close()
 def test_expired_quote_after_sqlite_wait_blocks_open(self):
  held=threading.Event();release=threading.Event()
  def lock():
   d=sqlite3.connect(self.fin,isolation_level=None);d.execute('BEGIN IMMEDIATE');held.set();release.wait(3);d.rollback();d.close()
  thread=threading.Thread(target=lock);thread.start();self.assertTrue(held.wait(2));timer=threading.Timer(.1,release.set);timer.start()
  try:
   with self.assertRaisesRegex(ValueError,'stale_quote'):self.call(clock=lambda:self.now+(6000 if release.is_set() else 0))
  finally:release.set();timer.cancel();thread.join()
  self.assertIsNone(self.store.get('position'));self.assertEqual(self.store.db.execute("SELECT count(*) FROM futures_events WHERE event='OPEN'").fetchone()[0],0)
 def test_missing_or_expired_funding_evidence_blocks_entry(self):
  for evidence in (None,dict(self.funding,complete=False),dict(self.funding,next_due_ms=self.now),dict(self.funding,observed_ms=self.now-120001)):
   with self.assertRaisesRegex(ValueError,'funding_evidence_unavailable'):self.call(funding_evidence=evidence)
  self.assertIsNone(self.store.get('position'))
 def test_unexpected_execution_costs_block_entry(self):
  self.store.fee=Decimal('1')
  with self.assertRaisesRegex(ValueError,'unexpected_execution_costs'):self.call()
  self.assertIsNone(self.store.get('position'))
 def test_minimum_notional_does_not_increase_risk(self):
  import copy
  filters=copy.deepcopy(self.filters)
  for item in filters['filters']:
   if item['filterType']=='MIN_NOTIONAL':item['minNotional']='11'
  with self.assertRaises(ValueError):self.call(filters=filters)
  self.assertIsNone(self.store.get('position'))
 def test_spot_position_blocks_conflicting_short_entry(self):
  s=RuntimeStore(self.fin,{'mode':'paper','symbol':'BTCUSDT'},shared=True)
  try:
   app=Application(s,PaperExchange(),None);s.command(1,True,'/resume');app.execute('BUY','.1',Decimal('100'),'other','TEST')
   with self.assertRaisesRegex(ValueError,'position_exists'):self.call()
   self.assertIsNone(self.store.get('position'))
  finally:s.close()
 def test_open_time_is_sampled_after_lock(self):
  ticks=iter((self.now,self.now+1,self.now+2))
  self.assertEqual(self.call(clock=lambda:next(ticks))['status'],'filled')
  self.assertEqual(self.store.get('position')['opened_ms'],self.now+2)
 def test_worker_short_funding_stop_and_lost_ack(self):
  state=RouteState(self.fin.parent/'route',clock=lambda:self.now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'SHORT'}
  args={'decision':self.decision,'signal':signal,'data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':self.filters,'filters_ms':self.now,'funding_evidence':self.funding,'clock':lambda:self.now}
  def crash(stage):
   if stage=='before_queue_ack':raise RuntimeError('lost ack')
  try:
   with self.assertRaisesRegex(RuntimeError,'lost ack'):process_short(self.q,self.store,state,**args,fault=crash)
   self.assertEqual(process_short(self.q,self.store,state,**args)['status'],'OPEN');position=self.store.get('position')
   unavailable=observe(self.q,self.store,state,quote='103',quote_ms=self.now+1000,clock=lambda:self.now+1000)
   self.assertEqual((unavailable['state'],unavailable['reason']),('unavailable','funding_coverage_missing'))
   self.assertEqual(unavailable['trigger'],'stop')
   self.assertEqual(unavailable['funding_wait']['trigger'],'stop')
   # A restart keeps the observed but unexecuted trigger; an old price is
   # never substituted for the fresh quote needed by a later close.
   self.q.close();self.q=UnifiedQueue(self.qpath);self.addCleanup(self.q.close)
   still_waiting=observe(self.q,self.store,state,quote='100',quote_ms=self.now+1000,clock=lambda:self.now+1000)
   self.assertEqual(still_waiting['funding_wait']['trigger'],'stop')
   self.assertIsNone(still_waiting['trigger'])
   self.assertFalse(any(j['command']=='/research_adaptive_exit' for j in self.q.pending('futures')))
   self.store.apply_funding(position['id'],[{'time':self.now+1000,'rate':'.001','mark':'100'}],self.now+1000)
   self.store.command(2,True,'/pause')
   self.assertEqual(observe(self.q,self.store,state,quote='103',quote_ms=self.now+1000,clock=lambda:self.now+1000)['trigger'],'stop')
   exitargs={'quote':'103','quote_ms':self.now+1000,'clock':lambda:self.now+1000}
   with self.assertRaisesRegex(RuntimeError,'lost ack'):process_short_stop(self.q,self.store,state,**exitargs,fault=crash)
   self.assertEqual(process_short_stop(self.q,self.store,state,**exitargs)['status'],'IDLE')
   self.assertTrue(self.store.get('paused'));self.assertIsNone(self.store.get('position'));self.assertTrue(self.store.shared.ledger.reconcile()['balanced'])
   self.assertEqual(self.store.db.execute("SELECT count(*) FROM futures_events WHERE event='CLOSE'").fetchone()[0],1)
  finally:state.close()
 def test_short_reverse_channel_exit_after_fixed_delay(self):
  state=RouteState(self.fin.parent/'route',clock=lambda:self.now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'SHORT'}
  args={'decision':self.decision,'signal':signal,'data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':self.filters,'filters_ms':self.now,'funding_evidence':self.funding,'clock':lambda:self.now}
  try:
   process_short(self.q,self.store,state,**args);bar=900000
   bars=[((i+4)*bar,100.,101.,99.,100.) for i in range(196)];bars[-1]=(199*bar,100.,102.,99.,101.)
   now=201*bar;skip=(200*bar,100.,101.,99.,100.);self.store.apply_funding(self.store.get('position')['id'],[],now)
   self.assertEqual(observe(self.q,self.store,state,quote='100',quote_ms=now,bars=bars,skip_bar=skip,clock=lambda:now)['trigger'],'signal')
   self.assertEqual(process_short_stop(self.q,self.store,state,quote='100',quote_ms=now,clock=lambda:now)['status'],'IDLE')
   import json
   self.assertEqual(json.loads(self.store.db.execute("SELECT payload FROM futures_events WHERE event='CLOSE'").fetchone()[0])['reason'],'SIGNAL')
  finally:state.close()
 def test_process_death_after_short_commit_recovers_without_duplicate(self):
  if os.name!='posix':self.skipTest('POSIX process semantics required')
  child=r'''
import os,sys
from pathlib import Path
from tests.test_paper_pair_catalog import spot_info
from trading_bot.futures_store import FuturesStore
from trading_bot.unified_queue import UnifiedQueue
from research.adaptive_route_state import RouteState
from research.adaptive_spot_worker import process_short
p=Path(sys.argv[1]);now=197*900000
s=FuturesStore(p/'financial',shared=True,fee_bps='10',slippage_bps='5');q=UnifiedQueue(p/'queue');state=RouteState(p/'route',clock=lambda:now)
decision={'mode':'TREND_DOWN','snapshot_id':'s','data_ms':now-1000,'expires_ms':now+10000,'reasons':['fixture'],'uncertainties':['fixture']}
signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'SHORT'}
bars=[(i*900000,100.,101.,99.,100.) for i in range(196)];bars[-1]=(195*900000,100.,101.,98.,99.)
filters=spot_info('BTC');filters.update(contractType='PERPETUAL',marginAsset='USDT')
def fail(stage):
 if stage=='after_open':os._exit(73)
funding={'symbol':'BTCUSDT','complete':True,'observed_ms':now,'next_due_ms':now+28800000,'source':'synthetic'}
process_short(q,s,state,decision=decision,signal=signal,data_ms=now-1000,decision_version='regular-regime-horizon-v2',bars=bars,skip_bar=(196*900000,100.,101.,99.,100.),quote='100',quote_ms=now,filters=filters,filters_ms=now,funding_evidence=funding,clock=lambda:now,fault=fail)
'''
  self.assertEqual(subprocess.run([sys.executable,'-B','-c',child,str(self.fin.parent)],timeout=20).returncode,73)
  state=RouteState(self.fin.parent/'route',clock=lambda:self.now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'SHORT'}
  args={'decision':self.decision,'signal':signal,'data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':self.filters,'filters_ms':self.now,'funding_evidence':self.funding,'clock':lambda:self.now}
  try:
   self.assertEqual(process_short(self.q,self.store,state,**args)['status'],'OPEN')
   self.assertEqual(self.store.db.execute("SELECT count(*) FROM futures_events WHERE event='OPEN'").fetchone()[0],1);self.assertTrue(self.store.shared.ledger.reconcile()['balanced'])
  finally:state.close()
 def test_backup_restores_short_after_fill_before_ack(self):
  state=RouteState(self.fin.parent/'route',clock=lambda:self.now)
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'SHORT'}
  args={'decision':self.decision,'signal':signal,'data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':self.filters,'filters_ms':self.now,'funding_evidence':self.funding,'clock':lambda:self.now}
  def crash(stage):
   if stage=='before_queue_ack':raise RuntimeError('lost ack')
  try:
   with self.assertRaisesRegex(RuntimeError,'lost ack'):process_short(self.q,self.store,state,**args,fault=crash)
   obs=self.fin.parent/'observation';db=sqlite3.connect(obs);db.execute('create table samples(id integer primary key)');db.commit();db.close()
   bundle=self.fin.parent/'backup';backup(self.fin,self.qpath,obs,self.fin.parent/'route',bundle);verify(bundle)
   f=FuturesStore(bundle/'financial.db',shared=True,fee_bps='10',slippage_bps='5');q=UnifiedQueue(bundle/'queue.db');r=RouteState(bundle/'route.db',clock=lambda:self.now)
   try:
    f.command(8,True,'/pause')
    self.assertEqual(process_short(q,f,r,**args)['status'],'OPEN');self.assertTrue(f.get('paused'))
    self.assertEqual(f.db.execute("SELECT count(*) FROM futures_events WHERE event='OPEN'").fetchone()[0],1);self.assertTrue(f.shared.ledger.reconcile()['balanced'])
   finally:r.close();q.close();f.close()
  finally:state.close()
 def test_confirmed_manual_close_after_mode_change_is_reconciled(self):
  state=RouteState(self.fin.parent/'route',clock=lambda:self.now);receiver=MarketReceiver(self.fin,'futures')
  signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'SHORT'}
  args={'decision':self.decision,'signal':signal,'data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':(196*900000,100.,101.,99.,100.),'quote':'100','quote_ms':self.now,'filters':self.filters,'filters_ms':self.now,'funding_evidence':self.funding,'clock':lambda:self.now}
  try:
   process_short(self.q,self.store,state,**args);position=self.store.get('position')
   newer=dict(self.decision,mode='TREND_UP',snapshot_id='next',data_ms=self.now,expires_ms=self.now+10000)
   record(self.q,newer,source='AI',symbol='BTCUSDT',version='regular-regime-horizon-v2',now_ms=self.now)
   self.q.receive(2,True,'/mode futures');self.q.receive(3,True,'/close',positions={'futures':position});process_market(self.q,receiver,self.store,'futures')
   token=self.q.db.execute("SELECT id FROM unified_proposals WHERE status='OPEN'").fetchone()[0]
   self.q.receive(4,True,'/confirm '+token);process_market(self.q,receiver,self.store,'futures')
   self.store.apply_funding(position['id'],[],self.now+1000)
   self.assertTrue(self.store.close_position(Decimal('100'),self.now+1000,'manual-close','MANUAL',position['id']))
   self.assertEqual(reconcile_futures(self.q,self.store,state)['phase'],'IDLE');self.assertTrue(self.store.get('paused'))
   self.assertEqual(self.store.db.execute("SELECT count(*) FROM futures_events WHERE event='CLOSE'").fetchone()[0],1)
  finally:receiver.close();state.close()
