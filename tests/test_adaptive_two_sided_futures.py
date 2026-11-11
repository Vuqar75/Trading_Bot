"""Direction-aware research execution, using existing temporary ledger fixtures."""
import copy
import json
import sqlite3
import threading
import unittest
from decimal import Decimal

from tests import test_adaptive_atomic_futures as fixtures
from research.adaptive_decision_queue import enqueue
from research.adaptive_route_policy import route, VERSION, FUTURES_VERSION
from research.adaptive_route_state import RouteState
from research.adaptive_spot_worker import process_futures_entry, process_short_stop
from research.adaptive_protection_monitor import observe
from research.adaptive_sandbox_backup import backup, verify
from trading_bot.futures_store import FuturesStore
from trading_bot.unified_queue import UnifiedQueue


class TwoSidedFuturesTest(unittest.TestCase):
 def setUp(self):
  fixtures.AtomicFuturesTest.setUp(self)
  self.state=RouteState(self.fin.parent/'route',clock=lambda:self.now)
  self.addCleanup(self.state.close)
  self.prepare('LONG')

 def prepare(self,direction):
  self.direction=direction
  self.decision['mode']='TREND_UP' if direction=='LONG' else 'TREND_DOWN'
  self.bars[-1]=(195*900000,100.,102.,98.,101. if direction=='LONG' else 99.)
  with self.q.transaction():
   self.q.db.execute('DELETE FROM unified_jobs')
   self.q.db.execute('DELETE FROM unified_updates')
   self.q.db.execute('DELETE FROM unified_state')
  self.job=enqueue(self.q,self.decision,source='AI',symbol='BTCUSDT',version='regular-regime-horizon-v2',now_ms=self.now,bars=self.bars,skip_bar=(196*900000,100.,101.,99.,100.),funding_evidence=self.funding,route_policy=FUTURES_VERSION)

 def args(self):
  return dict(direction=self.direction,decision=self.decision,
   signal={'source':'AI','symbol':'BTCUSDT','snapshot_id':'s','strategy':'trend1','direction':self.direction},
   data_ms=self.decision['data_ms'],decision_version='regular-regime-horizon-v2',bars=self.bars,
   skip_bar=(196*900000,100.,101.,99.,100.),quote='100',quote_ms=self.now,
   filters=self.filters,filters_ms=self.now,funding_evidence=self.funding,
   clock=lambda:self.now,route_policy=FUTURES_VERSION)

 def open(self,**changes):
  return process_futures_entry(self.q,self.store,self.state,**(self.args()|changes))

 def close(self,price,*,bars=None,skip_bar=None,risk_bar=None):
  self.store.apply_funding(self.store.get('position')['id'],[],self.now)
  summary=observe(self.q,self.store,self.state,quote=price,quote_ms=self.now,bars=bars,skip_bar=skip_bar,risk_bar=risk_bar,clock=lambda:self.now)
  result=process_short_stop(self.q,self.store,self.state,quote=price,quote_ms=self.now,clock=lambda:self.now)
  return summary,result

 def test_opt_in_only_and_range_stays_blocked(self):
  kw=dict(snapshot_id='s',data_ms=self.decision['data_ms'],now_ms=self.now,symbol='BTCUSDT',decision_version='regular-regime-horizon-v2',eligible=True,research_sandbox=True)
  self.assertEqual(route(self.decision,**kw)['market'],'spot')
  result=route(self.decision,**kw,policy=FUTURES_VERSION)
  self.assertEqual((result['market'],result['direction']),('futures','LONG'))
  self.assertEqual(route(dict(self.decision,mode='RANGE'),**kw,policy=FUTURES_VERSION)['reason'],'range_strategy_not_qualified')
  self.assertFalse(route(self.decision,**(kw|{'eligible':False}),policy=FUTURES_VERSION)['allowed'])
  self.assertFalse(route(self.decision,**(kw|{'research_sandbox':False}),policy=FUTURES_VERSION)['allowed'])

 def test_long_time_exit_uses_position_after_ai_expiry_and_pause(self):
  self.assertEqual(self.open()['status'],'OPEN')
  position=self.store.get('position')
  self.assertEqual(position['direction'],'LONG')
  self.assertEqual(Decimal(position['entry']),Decimal('100.05'))
  self.now+=604800000
  self.store.command(9,True,'/pause')
  summary,result=self.close('103')
  self.assertEqual(summary['trigger'],'time')
  self.assertEqual(result['status'],'IDLE')
  event=json.loads(self.store.db.execute("SELECT payload FROM futures_events WHERE event='CLOSE'").fetchone()[0])
  self.assertGreater(Decimal(event['gross']),0)
  self.assertTrue(self.store.get('paused'))
  self.assertTrue(self.store.shared.ledger.reconcile()['balanced'])
  self.assertEqual(self.store.db.execute("SELECT count(*) FROM runtime_orders").fetchone()[0],0)

 def test_long_stop_does_not_trigger_on_rising_price(self):
  self.open();self.now+=1000
  self.store.apply_funding(self.store.get('position')['id'],[],self.now)
  summary=observe(self.q,self.store,self.state,quote='103',quote_ms=self.now,clock=lambda:self.now)
  self.assertIsNone(summary['trigger'])
  summary,result=self.close('97')
  self.assertEqual(summary['trigger'],'stop')
  self.assertEqual(result['status'],'IDLE')
  self.assertLess(Decimal(self.store.get('pnl')),0)

 def test_long_intrabar_low_triggers_even_after_recovery(self):
  self.open();opened=self.now;self.now+=900000
  summary,result=self.close('101',risk_bar=(opened,100.,103.,97.,101.))
  self.assertEqual(summary['trigger'],'stop')
  self.assertEqual(result['status'],'IDLE')

 def test_long_lower_channel_exit(self):
  self.open();bar=900000;self.now=201*bar
  rows=[((i+4)*bar,100.,101.,99.,100.) for i in range(196)]
  rows[-1]=(199*bar,100.,101.,98.,99.)
  summary,result=self.close('100',bars=rows,skip_bar=(200*bar,100.,101.,99.,100.))
  self.assertEqual(summary['trigger'],'signal')
  self.assertEqual(result['status'],'IDLE')

 def test_short_on_new_policy_keeps_opposite_stop_and_funding_sign(self):
  self.prepare('SHORT');self.open();position=self.store.get('position')
  self.assertEqual(Decimal(position['entry']),Decimal('99.95'))
  self.now+=1000
  self.store.apply_funding(position['id'],[{'time':self.now,'rate':'.001','mark':'100'}],self.now)
  self.assertGreater(Decimal(self.store.get('position')['funding']),0)
  summary,result=self.close('103')
  self.assertEqual(summary['trigger'],'stop')
  self.assertEqual(result['status'],'IDLE')

 def test_funding_wait_keeps_long_open_until_accounting_is_complete(self):
  self.open();position=self.store.get('position');self.now+=1000
  summary=observe(self.q,self.store,self.state,quote='97',quote_ms=self.now,clock=lambda:self.now)
  self.assertEqual(summary['reason'],'funding_coverage_missing')
  self.assertEqual(summary['trigger'],'stop')
  self.assertEqual(self.store.get('position')['id'],position['id'])
  self.assertFalse([j for j in self.q.pending('futures') if j['command']=='/research_adaptive_exit'])
  self.store.apply_funding(position['id'],[{'time':self.now,'rate':'.001','mark':'100'}],self.now)
  self.assertLess(Decimal(self.store.get('position')['funding']),0)
  summary,result=self.close('97')
  self.assertEqual(result['status'],'IDLE')
  self.assertTrue(self.store.shared.ledger.reconcile()['balanced'])

 def test_policy_is_bound_to_queue_and_cannot_be_reinterpreted(self):
  with self.assertRaisesRegex(ValueError,'queue_policy_mismatch'):self.open(route_policy=VERSION)
  self.assertIsNone(self.store.get('position'))
  saved=json.loads(self.q.db.execute('SELECT payload FROM unified_jobs').fetchone()[0])
  saved['route_policy']=VERSION
  self.q.db.execute('UPDATE unified_jobs SET payload=?',(json.dumps(saved),))
  with self.assertRaisesRegex(ValueError,'queue_policy_mismatch'):self.open()
  self.assertIsNone(self.store.get('position'))

 def test_long_requires_upper_breakout_and_matching_direction(self):
  with self.assertRaisesRegex(ValueError,'signal_direction_mismatch'):self.open(direction='SHORT')
  self.bars[-1]=(195*900000,100.,101.,99.,100.)
  self.prepare_without_signal()
  self.assertEqual(self.open()['status'],'NO_SIGNAL')
  self.assertIsNone(self.store.get('position'))

 def test_existing_spot_exposure_blocks_long_under_financial_lock(self):
  from trading_bot.runtime_store import RuntimeStore
  from trading_bot.app import Application
  from trading_bot.runtime_exchange import PaperExchange
  store=RuntimeStore(self.fin,{'mode':'paper','symbol':'BTCUSDT'},shared=True)
  try:
   app=Application(store,PaperExchange(),None);store.command(1,True,'/resume')
   app.execute('BUY','.1',Decimal('100'),'existing-spot','TEST')
   with self.assertRaisesRegex(ValueError,'position_exists'):self.open()
   self.assertIsNone(self.store.get('position'))
   self.assertEqual(self.store.db.execute("SELECT count(*) FROM futures_events WHERE event='OPEN'").fetchone()[0],0)
   self.assertTrue(self.store.shared.ledger.reconcile()['balanced'])
  finally:store.close()

 def prepare_without_signal(self):
  with self.q.transaction():
   self.q.db.execute('DELETE FROM unified_jobs');self.q.db.execute('DELETE FROM unified_updates')
  enqueue(self.q,self.decision,source='AI',symbol='BTCUSDT',version='regular-regime-horizon-v2',now_ms=self.now,bars=self.bars,skip_bar=(196*900000,100.,101.,99.,100.),funding_evidence=self.funding,route_policy=FUTURES_VERSION)

 def test_stale_quote_after_write_lock_never_opens_long(self):
  held=threading.Event();release=threading.Event();base=self.now
  def lock():
   db=sqlite3.connect(self.fin,isolation_level=None)
   try:db.execute('BEGIN IMMEDIATE');held.set();release.wait(3);db.rollback()
   finally:db.close()
  thread=threading.Thread(target=lock);thread.start();self.assertTrue(held.wait(2))
  timer=threading.Timer(.1,release.set);timer.start()
  try:
   with self.assertRaisesRegex(ValueError,'stale_quote'):self.open(clock=lambda:base+(6000 if release.is_set() else 0))
  finally:release.set();timer.cancel();thread.join()
  self.assertIsNone(self.store.get('position'))
  self.assertTrue(self.store.shared.ledger.reconcile()['balanced'])

 def test_minimum_order_uses_buy_slippage_and_saves_proof(self):
  filters=copy.deepcopy(self.filters);filters['marketTakeBound']='0.03'
  for item in filters['filters']:
   if item['filterType']=='MIN_NOTIONAL':item['minNotional']='50'
  mark={'symbol':'BTCUSDT','source':'binance_usdm_mark','price':'100','event_ms':self.now,'received_ms':self.now}
  trade={'symbol':'BTCUSDT','source':'binance_usdm_trade','trade_id':7,'price':'100','event_ms':self.now,'received_ms':self.now}
  self.open(filters=filters,execution_fetch=lambda **kw:trade,filter_mark_proof=mark)
  pos=self.store.get('position');notional=Decimal(pos['entry'])*Decimal(pos['quantity'])
  self.assertGreaterEqual(Decimal(pos['quantity'])*100,50)
  self.assertLessEqual(notional,100)
  self.assertEqual(Decimal(pos['entry']),Decimal('100.05'))
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM adaptive_execution_proofs').fetchone()[0],1)

 def test_backup_recovers_long_fill_before_ack_without_second_open(self):
  def fail(stage):
   if stage=='before_queue_ack':raise RuntimeError('lost acknowledgement')
  with self.assertRaisesRegex(RuntimeError,'lost acknowledgement'):self.open(fault=fail)
  obs=self.fin.parent/'observation'
  with sqlite3.connect(obs) as db:db.execute('CREATE TABLE samples(id INTEGER PRIMARY KEY)')
  bundle=self.fin.parent/'backup'
  backup(self.fin,self.qpath,obs,self.fin.parent/'route',bundle);verify(bundle)
  f=FuturesStore(bundle/'financial.db',shared=True,fee_bps='10',slippage_bps='5')
  q=UnifiedQueue(bundle/'queue.db');r=RouteState(bundle/'route.db',clock=lambda:self.now)
  try:
   f.command(9,True,'/pause')
   self.assertEqual(process_futures_entry(q,f,r,**self.args())['status'],'OPEN')
   self.assertEqual(f.get('position')['direction'],'LONG')
   self.assertTrue(f.get('paused'))
   self.assertEqual(f.db.execute("SELECT count(*) FROM futures_events WHERE event='OPEN'").fetchone()[0],1)
   self.assertTrue(f.shared.ledger.reconcile()['balanced'])
  finally:r.close();q.close();f.close()
