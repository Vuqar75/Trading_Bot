import tempfile,unittest,sqlite3,threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from research.adaptive_study_supervisor import bootstrap
from research.adaptive_market_epoch import start_epoch,ingest_epoch
from research.adaptive_route_state import RouteState
from research.adaptive_decision_queue import enqueue
from research.adaptive_atomic_futures import enter_short
from research.adaptive_spot_worker import process_short,process_short_stop
from research.adaptive_experiment_recovery import reconcile
from research.adaptive_funding_tracker import register,advance
from research.adaptive_protection_monitor import observe
from trading_bot.unified_queue import UnifiedQueue
from trading_bot.futures_store import FuturesStore
from tests.test_paper_pair_catalog import spot_info
class V2AtomicStudyTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)/'study';bar=900000;self.now=197*bar
  bootstrap(self.root,clock=lambda:1000);start_epoch(self.root,market='futures',clock=lambda:194*bar)
  self.path=self.root/'ai';self.q=UnifiedQueue(self.path/'queue.db');self.addCleanup(self.q.close)
  self.state=RouteState(self.path/'route.db',clock=lambda:self.now);self.addCleanup(self.state.close)
  self.store=FuturesStore(self.path/'financial.db',shared=True,fee_bps='10',slippage_bps='5');self.addCleanup(self.store.close)
  self.bars=[(i*bar,100.,101.,99.,100.) for i in range(196)];self.bars[-1]=(195*bar,100.,101.,98.,99.);self.skip=(196*bar,100.,101.,99.,100.)
  candles={'market':'futures','symbol':'BTCUSDT','source':'binance_usdm_klines','bars':self.bars,'skip_bar':self.skip}
  candles['raw_bars']=[[r[0],str(r[1]),str(r[2]),str(r[3]),str(r[4]),'10',r[0]+899999] for r in self.bars+[self.skip]]
  d=sqlite3.connect(self.root/'market-evidence.db',isolation_level=None);self.addCleanup(d.close);ingest_epoch(d,market='futures',candles=candles,observed_ms=self.now)
  from research.adaptive_candle_confirmation import compare,record_event
  def warm(t):return {'market':'futures','symbol':'BTCUSDT','source':'binance_usdm_klines','raw_bars':candles['raw_bars'][:-1],'received_ms':t,'server_ms':t-1,'bars':self.bars[:-1],'skip_bar':self.bars[-1]}
  confirmation=compare(warm(self.now-45000),warm(self.now-30000));record_event(d,1,{'state':'accepted','sample':confirmation['second'],'comparison':confirmation},clock=lambda:self.now-29000)
  self.decision={'mode':'TREND_DOWN','snapshot_id':'s','data_ms':self.now-1000,'expires_ms':self.now+10000,'reasons':['x'],'uncertainties':['x']}
  self.funding={'symbol':'BTCUSDT','complete':True,'observed_ms':self.now,'next_due_ms':self.now+28800000,'source':'synthetic'}
  self.job=enqueue(self.q,self.decision,source='AI',symbol='BTCUSDT',version='regular-regime-horizon-v2',now_ms=self.now,bars=self.bars,skip_bar=self.skip,funding_evidence=self.funding)
  self.action=self.job['action_id'];self.signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':'SHORT'}
  self.state.begin_entry(self.action,self.decision,self.signal,snapshot_id='s',data_ms=self.now-1000,symbol='BTCUSDT',decision_version='regular-regime-horizon-v2',quote_ms=self.now,expiry_ms=self.now+5000,eligible=True,research_sandbox=True)
  self.filters=spot_info('BTC');self.filters.update(contractType='PERPETUAL',marginAsset='USDT',marketTakeBound='0.03')
  for row in self.filters['filters']:
   if row['filterType']=='MIN_NOTIONAL':row['minNotional']='50'
  self.proof={'source':'binance_usdm_trade','symbol':'BTCUSDT','trade_id':5,'price':'100','event_ms':self.now,'received_ms':self.now}
  self.mark={'source':'binance_usdm_mark','symbol':'BTCUSDT','price':'100','event_ms':self.now,'received_ms':self.now}
 def call(self,**kw):
  args={'job_id':self.job['job_id'],'action_id':self.action,'decision':self.decision,'snapshot_id':'s','data_ms':self.now-1000,'decision_version':'regular-regime-horizon-v2','bars':self.bars,'skip_bar':self.skip,'quote':'100','quote_ms':self.now,'filters':self.filters,'filters_ms':self.now,'funding_evidence':self.funding,'decision_source':'AI','clock':lambda:self.now,'execution_proof':self.proof,'signal_fixed_ms':self.now,'filter_mark_proof':self.mark};args.update(kw)
  return enter_short(self.store,self.path/'queue.db',**args)
 def test_epoch_proof_and_minimum_size(self):
  self.assertEqual(self.call()['status'],'filled');self.assertGreater(float(self.store.get('position')['margin']),49);self.assertTrue(self.store.shared.ledger.reconcile()['balanced'])
  self.assertEqual(self.call()['status'],'filled_existing')
 def test_source_and_epoch_conflict_block(self):
  self.proof['source']='binance_usdm_mark'
  with self.assertRaisesRegex(ValueError,'trade_source_invalid'):self.call()
  self.proof['source']='binance_usdm_trade'
  d=sqlite3.connect(self.root/'market-evidence.db');d.execute("INSERT INTO epoch_conflicts VALUES(1,0,'conflict',?)",(self.now,));d.commit();d.close()
  with self.assertRaisesRegex(ValueError,'v2_epoch_conflict'):self.call()
  self.assertIsNone(self.store.get('position'))
 def test_confirmation_must_be_durable_before_signal(self):
  d=sqlite3.connect(self.root/'market-evidence.db')
  d.execute('UPDATE candle_confirmations SET recorded_ms=NULL');d.commit()
  with self.assertRaisesRegex(ValueError,'v2_warm_confirmation_missing'):self.call()
  d.execute('UPDATE candle_confirmations SET recorded_ms=?',(self.now+1,));d.commit()
  with self.assertRaisesRegex(ValueError,'v2_warm_confirmation_missing'):self.call()
  self.assertIsNone(self.store.get('position'));d.close()
 def test_signal_before_epoch_is_not_executed(self):
  d=sqlite3.connect(self.root/'market-evidence.db')
  d.execute('UPDATE market_epochs SET start_ms=? WHERE id=1',(self.bars[-1][0]+900000,));d.commit();d.close()
  with self.assertRaisesRegex(ValueError,'signal_predates_epoch'):self.call()
  self.assertIsNone(self.store.get('position'))
 def test_stale_trade_after_financial_lock_wait_cannot_fill(self):
  held=threading.Event();release=threading.Event()
  def lock():
   db=sqlite3.connect(self.path/'financial.db',isolation_level=None);db.execute('BEGIN IMMEDIATE');held.set();release.wait(3);db.rollback();db.close()
  thread=threading.Thread(target=lock);thread.start();self.assertTrue(held.wait(2));timer=threading.Timer(.1,release.set);timer.start()
  try:
   with self.assertRaisesRegex(ValueError,'stale_quote_or_signal'):self.call(clock=lambda:self.now+(6000 if release.is_set() else 0))
  finally:release.set();timer.cancel();thread.join()
  self.assertIsNone(self.store.get('position'))
 def test_cancel_locks_route_and_finance_against_later_fill(self):
  self.assertEqual(self.state.confirm_futures_not_filled_v2(self.action,self.path/'financial.db')['phase'],'IDLE')
  with self.assertRaisesRegex(ValueError,'v2_route_not_pending'):self.call()
  self.assertIsNone(self.store.get('position'))
 def test_committed_fill_cannot_be_canceled(self):
  self.assertEqual(self.call()['status'],'filled')
  with self.assertRaisesRegex(ValueError,'financial_event_exists'):self.state.confirm_futures_not_filled_v2(self.action,self.path/'financial.db')
 def test_competing_cancel_and_entry_never_leave_canceled_fill(self):
  gate=threading.Barrier(2)
  def enter():
   store=FuturesStore(self.path/'financial.db',shared=True,fee_bps='10',slippage_bps='5')
   try:
    gate.wait(timeout=3)
    return enter_short(store,self.path/'queue.db',job_id=self.job['job_id'],action_id=self.action,decision=self.decision,snapshot_id='s',data_ms=self.now-1000,decision_version='regular-regime-horizon-v2',bars=self.bars,skip_bar=self.skip,quote='100',quote_ms=self.now,filters=self.filters,filters_ms=self.now,funding_evidence=self.funding,decision_source='AI',clock=lambda:self.now,execution_proof=self.proof,signal_fixed_ms=self.now,filter_mark_proof=self.mark)['status']
   except (ValueError,sqlite3.OperationalError):return 'blocked'
   finally:store.close()
  def cancel():
   state=RouteState(self.path/'route.db',clock=lambda:self.now)
   try:
    gate.wait(timeout=3);return state.confirm_futures_not_filled_v2(self.action,self.path/'financial.db')['phase']
   except (ValueError,sqlite3.OperationalError):return 'blocked'
   finally:state.close()
  with ThreadPoolExecutor(max_workers=2) as pool:
   a=pool.submit(enter);b=pool.submit(cancel);results=(a.result(timeout=15),b.result(timeout=15))
  count=self.store.db.execute("SELECT count(*) FROM futures_events WHERE event='OPEN'").fetchone()[0]
  self.assertLessEqual(count,1);self.assertFalse('IDLE' in results and count);self.assertTrue(self.store.shared.ledger.reconcile()['balanced'])
 def worker(self,fetch,fault=lambda _:None):
  self.state.db.execute('DELETE FROM route_actions');self.state.db.execute("UPDATE route_state SET phase='IDLE',action_id=NULL,strategy=NULL,market=NULL,direction=NULL,position_id=NULL WHERE id=1")
  return process_short(self.q,self.store,self.state,decision=self.decision,signal=self.signal,data_ms=self.now-1000,decision_version='regular-regime-horizon-v2',bars=self.bars,skip_bar=self.skip,quote='100',quote_ms=self.now,filters=self.filters,filters_ms=self.now,funding_evidence=self.funding,clock=lambda:self.now,execution_fetch=fetch,filter_mark_proof=self.mark,fault=fault)
 def test_worker_post_intent_trade_and_unavailable(self):
  result=self.worker(lambda **_:self.proof);self.assertEqual(result['status'],'OPEN')
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM adaptive_execution_proofs').fetchone()[0],1)
 def test_unavailable_quote_cancels_without_fill(self):
  def missing(**kw):raise ValueError('unavailable')
  self.assertEqual(self.worker(missing)['status'],'CANCELED');self.assertIsNone(self.store.get('position'));self.assertEqual(self.state.status()['phase'],'IDLE')
 def test_crash_after_intent_cancels_without_backfill(self):
  def crash(stage):
   if stage=='after_route_commit':raise RuntimeError('crash')
  with self.assertRaisesRegex(RuntimeError,'crash'):self.worker(lambda **_:self.proof,fault=crash)
  self.assertEqual(reconcile(self.q,self.state,self.path/'financial.db',now_ms=self.now)['status'],'canceled')
  self.assertIsNone(self.store.get('position'))
 def test_crash_after_financial_entry_recovers_without_duplicate(self):
  def crash(stage):
   if stage=='after_open':raise RuntimeError('lost ack')
  with self.assertRaisesRegex(RuntimeError,'lost ack'):self.worker(lambda **_:self.proof,fault=crash)
  self.assertEqual(reconcile(self.q,self.state,self.path/'financial.db',now_ms=self.now)['status'],'open')
  self.assertEqual(self.store.db.execute("SELECT count(*) FROM futures_events WHERE event='OPEN'").fetchone()[0],1)
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM adaptive_execution_proofs').fetchone()[0],1)
 def test_protection_waits_then_closes_using_post_trigger_trade(self):
  self.worker(lambda **_:self.proof);position=self.store.get('position')
  register(self.store,position_id=position['id'],next_due_ms=self.now+28800000,observed_ms=self.now)
  mark={'symbol':'BTCUSDT','source':'binance_usdm_mark','price':'103','event_ms':self.now,'received_ms':self.now}
  summary=observe(self.q,self.store,self.state,quote='103',quote_ms=self.now,source_mark=mark,clock=lambda:self.now)
  self.assertEqual(summary['trigger'],'stop')
  def missing(**kw):raise ValueError('unavailable')
  self.assertEqual(process_short_stop(self.q,self.store,self.state,quote='103',quote_ms=self.now,clock=lambda:self.now,execution_fetch=missing)['status'],'EXIT_PENDING')
  self.assertEqual(reconcile(self.q,self.state,self.path/'financial.db',now_ms=self.now)['status'],'exit_pending')
  outside=dict(self.proof,price='110',trade_id=8)
  blocked=process_short_stop(self.q,self.store,self.state,quote='103',quote_ms=self.now,clock=lambda:self.now,execution_fetch=lambda **_:outside,current_mark_proof=mark,execution_filters=self.filters,filters_ms=self.now)
  self.assertEqual((blocked['status'],blocked['reason']),('EXIT_PENDING','market_take_bound_exceeded'));self.assertIsNotNone(self.store.get('position'))
  trade=dict(self.proof,price='102.5',trade_id=6)
  self.assertEqual(process_short_stop(self.q,self.store,self.state,quote='103',quote_ms=self.now,clock=lambda:self.now,execution_fetch=lambda **_:trade,current_mark_proof=mark,execution_filters=self.filters,filters_ms=self.now)['status'],'IDLE')
  self.assertIsNone(self.store.get('position'));self.assertTrue(self.store.shared.ledger.reconcile()['balanced'])
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM adaptive_execution_proofs').fetchone()[0],2)
 def test_late_funding_preserves_trigger_after_price_recovers(self):
  self.worker(lambda **_:self.proof);position=self.store.get('position');due=self.now+1000
  register(self.store,position_id=position['id'],next_due_ms=due,observed_ms=self.now)
  self.now=due+10;trigger={'symbol':'BTCUSDT','source':'binance_usdm_mark','price':'103','event_ms':self.now,'received_ms':self.now}
  pending=observe(self.q,self.store,self.state,quote='103',quote_ms=self.now,source_mark=trigger,clock=lambda:self.now)
  self.assertEqual(pending['reason'],'funding_coverage_missing');self.assertEqual(pending['funding_wait']['trigger'],'stop')
  self.now+=1000;row={'symbol':'BTCUSDT','fundingTime':due+1,'fundingRate':'0.001','markPrice':'100'}
  history={'source':'binance_usdm_funding_rate','symbol':'BTCUSDT','start_ms':due-1,'end_ms':self.now,'received_ms':self.now,'rows':[row]}
  advance(self.store,position_id=position['id'],mark_response={'symbol':'BTCUSDT','time':self.now,'markPrice':'100','nextFundingTime':due+28800000},received_ms=self.now,history=history)
  current={'symbol':'BTCUSDT','source':'binance_usdm_mark','price':'100','event_ms':self.now,'received_ms':self.now}
  resumed=observe(self.q,self.store,self.state,quote='100',quote_ms=self.now,source_mark=current,clock=lambda:self.now)
  self.assertEqual(resumed['trigger'],'stop');self.assertIn('job_id',resumed)
  trade={'symbol':'BTCUSDT','source':'binance_usdm_trade','trade_id':7,'price':'100','event_ms':self.now,'received_ms':self.now}
  self.assertEqual(process_short_stop(self.q,self.store,self.state,quote='100',quote_ms=self.now,clock=lambda:self.now,execution_fetch=lambda **_:trade,current_mark_proof=current,execution_filters=self.filters,filters_ms=self.now)['status'],'IDLE')
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM futures_funding').fetchone()[0],1);self.assertTrue(self.store.shared.ledger.reconcile()['balanced'])
 def test_crash_after_exit_commit_recovers_without_duplicate(self):
  self.worker(lambda **_:self.proof);position=self.store.get('position');register(self.store,position_id=position['id'],next_due_ms=self.now+28800000,observed_ms=self.now)
  mark={'symbol':'BTCUSDT','source':'binance_usdm_mark','price':'103','event_ms':self.now,'received_ms':self.now}
  observe(self.q,self.store,self.state,quote='103',quote_ms=self.now,source_mark=mark,clock=lambda:self.now)
  def crash(stage):
   if stage=='after_close':raise RuntimeError('lost exit ack')
  trade=dict(self.proof,price='103',trade_id=6)
  with self.assertRaisesRegex(RuntimeError,'lost exit ack'):process_short_stop(self.q,self.store,self.state,quote='103',quote_ms=self.now,clock=lambda:self.now,execution_fetch=lambda **_:trade,current_mark_proof=mark,execution_filters=self.filters,filters_ms=self.now,fault=crash)
  self.assertEqual(reconcile(self.q,self.state,self.path/'financial.db',now_ms=self.now)['status'],'closed')
  self.assertEqual(self.store.db.execute("SELECT count(*) FROM futures_events WHERE event='CLOSE'").fetchone()[0],1)
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM adaptive_execution_proofs').fetchone()[0],2)
if __name__=='__main__':unittest.main()
