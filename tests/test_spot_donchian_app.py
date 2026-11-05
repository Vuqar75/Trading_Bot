import sqlite3,tempfile,threading,time,unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from trading_bot.runtime_store import RuntimeStore
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.spot_donchian_app import DonchianSpotApplication
from trading_bot.spot_donchian import DAY_MS
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.unified_queue import UnifiedQueue
from tools.donchianspot_preconditions import check
from trading_bot.telegram_reports import history,status
from trading_bot.paper_pair_runtime_offline import ExperimentalSpotRunner

class Clock:
 def __init__(self,t):self.t=t
 def __call__(self):self.t+=.01;return self.t
class Reader:
 def __init__(self,clock,b):self.clock=clock;self.boundary=b;self.breakout=True
 def get(self,path,params=None):
  if path.endswith('/time'):return {'serverTime':int(self.clock()*1000)}
  if path.endswith('/ticker/price'):return {'symbol':'BTCUSDT','price':'100'}
  if path.endswith('/trades'):return [{'id':1,'time':int(self.clock()*1000),'price':'100'}]
  if path.endswith('/klines'):
   return [[t,'100','101','99','101' if self.breakout and t==self.boundary-DAY_MS else '100','1',t+DAY_MS-1] for t in range(self.boundary-90*DAY_MS,self.boundary,DAY_MS)]
  if path.endswith('/exchangeInfo'):return {'symbols':[{'symbol':'BTCUSDT','status':'TRADING','quoteAsset':'USDT','baseAsset':'BTC','orderTypes':['MARKET'],'filters':[{'filterType':'LOT_SIZE','minQty':'0.00001','maxQty':'10000','stepSize':'0.00001'},{'filterType':'MIN_NOTIONAL','minNotional':'5'}]}]}
  raise ValueError(path)
class DonchianAppTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.b=20000*DAY_MS;self.clock=Clock((self.b-1000)/1000);self.reader=Reader(self.clock,self.b);self.store=RuntimeStore(Path(self.tmp.name)/'test.db',{'mode':'paper','symbol':'BTCUSDT','interval':'15m','amount':'10'});self.addCleanup(self.store.close)
  self.app=DonchianSpotApplication(self.store,PaperExchange(),self.reader,clock=self.clock)
 def test_entry_once_and_recovery(self):
  with self.store.transaction():self.store.set('paused',False)
  self.clock.t=self.b/1000+1
  with patch('trading_bot.runtime_store.time.time',self.clock):self.app.tick();self.app.tick()
  self.assertGreater(float(self.store.get('position')['quantity']),0);self.assertEqual(self.store.db.execute('select count(*) from runtime_orders').fetchone()[0],1);self.assertEqual(self.store.get('donchian60')['phase'],'waiting')
  with self.store.transaction():self.store.set('paused',True)
  self.reader.boundary+=DAY_MS;self.reader.breakout=False;self.clock.t=self.reader.boundary/1000+1
  with patch('trading_bot.runtime_store.time.time',self.clock):self.app.tick();self.app.tick()
  self.assertEqual(float(self.store.get('position')['quantity']),0);self.assertEqual(self.store.db.execute('select count(*) from runtime_orders').fetchone()[0],2);self.assertTrue(self.store.get('paused'))
  self.assertIn('Дневной выход Donchian60',history(self.store))
 def test_paused_signal_not_backfilled(self):
  self.clock.t=self.b/1000+1
  with patch('trading_bot.runtime_store.time.time',self.clock):self.app.tick()
  self.assertEqual(self.store.db.execute('select count(*) from runtime_orders').fetchone()[0],0);self.assertEqual(self.store.get('donchian60')['last_result'],'entry_skipped_paused')
 def test_missed_window_blocks_entry(self):
  self.clock.t=(self.b+300001)/1000
  with patch('trading_bot.runtime_store.time.time',self.clock):self.app.tick()
  self.assertTrue(self.store.get('donchian60')['halted']);self.assertTrue(self.store.get('paused'))
 def test_manual_close_remains_available_on_pause(self):
  with self.store.transaction():self.store.set('paused',False)
  self.clock.t=self.b/1000+1
  with patch('trading_bot.runtime_store.time.time',self.clock):self.app.tick()
  pos=self.store.get('position')
  with self.store.transaction():self.store.set('paused',True);self.store.set('close_request',{'target':pos['id'],'update_id':17})
  with patch('trading_bot.runtime_store.time.time',self.clock):self.app.tick()
  self.assertEqual(float(self.store.get('position')['quantity']),0);self.assertTrue(self.store.get('paused'));self.assertIsNone(self.store.get('donchian60')['level'])
 def test_crash_before_execution_blocks_automatic_replay(self):
  with self.store.transaction():
   state=self.store.get('donchian60');state['phase']='executing';self.store.set('donchian60',state);self.store.set('paused',False)
  app=DonchianSpotApplication(self.store,PaperExchange(),self.reader,clock=self.clock)
  self.assertTrue(self.store.get('paused'));self.assertTrue(self.store.get('donchian60')['halted']);self.assertEqual(self.store.get('donchian60')['error'],'interrupted_decision_requires_review')
 def test_minimum_notional_skips_without_increasing_budget(self):
  original=self.reader.get
  def get(path,params=None):
   result=original(path,params)
   if path.endswith('/exchangeInfo'):result['symbols'][0]['filters'][1]['minNotional']='11'
   return result
  self.reader.get=get
  with self.store.transaction():self.store.set('paused',False)
  self.clock.t=self.b/1000+1
  with patch('trading_bot.runtime_store.time.time',self.clock):self.app.tick()
  self.assertEqual(self.store.get('donchian60')['last_result'],'entry_skipped_exchange_filter');self.assertEqual(self.store.db.execute('select count(*) from runtime_orders').fetchone()[0],0)
  self.assertIn('не проходит биржевые фильтры',status(self.store))
 def test_quote_is_rechecked_after_real_sqlite_lock_wait(self):
  now=time.time()
  with self.store.transaction():
   state=self.store.get('donchian60');state.update(phase='executing',boundary_ms=int(now*1000),quote_trade_ms=int(now*1000));self.store.set('donchian60',state);self.store.set('paused',False)
  locked=threading.Event()
  def holder():
   d=sqlite3.connect(str(Path(self.tmp.name)/'test.db'),isolation_level=None);d.execute('BEGIN IMMEDIATE');locked.set();time.sleep(5.3);d.rollback();d.close()
  t=threading.Thread(target=holder);t.start();self.assertTrue(locked.wait(5));self.store.db.execute('pragma busy_timeout=7000')
  try:
   with self.assertRaisesRegex(Exception,'expired after database wait'):self.store.prepare('BUY','0.01',__import__('decimal').Decimal('100'),'test','DONCHIAN60_ENTRY',quote_at=now,expected_symbol='BTCUSDT',expected_pair_version=0)
  finally:t.join()
  self.assertEqual(self.store.db.execute('select count(*) from runtime_orders').fetchone()[0],0)
 def test_quote_expiring_between_prepare_and_claim_is_not_executed(self):
  t=time.time()
  with self.store.transaction():
   state=self.store.get('donchian60');state.update(phase='executing',boundary_ms=int(t*1000),quote_trade_ms=int(t*1000));self.store.set('donchian60',state);self.store.set('paused',False)
  with patch('trading_bot.runtime_store.time.time',return_value=t):client=self.store.prepare('BUY','0.01',Decimal('100'),'expiry','DONCHIAN60_ENTRY',quote_at=t,expected_symbol='BTCUSDT',expected_pair_version=0)
  with patch('trading_bot.runtime_store.time.time',return_value=t+6):self.assertIsNone(self.store.claim(client,expected_symbol='BTCUSDT',expected_pair_version=0))
  self.assertEqual(self.store.db.execute('select phase,status from runtime_orders').fetchone()[:],('SETTLED','EXPIRED'));self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],0)
 def test_shared_ledger_entry_exit_reconcile_and_preserve_history(self):
  root=Path(self.tmp.name);f=FuturesStore(root/'futures.db');f.close();self.store.close()
  base=RuntimeStore(root/'base.db',{'mode':'paper','symbol':'BTCUSDT','interval':'15m','amount':'10'});base.close()
  consolidate_snapshots(root/'base.db',root/'futures.db',root/'shared.db',captured_at=self.clock())
  self.store=RuntimeStore(root/'shared.db',{'mode':'paper','symbol':'BTCUSDT','interval':'15m','amount':'10'},shared=True)
  self.addCleanup(self.store.close)
  q=UnifiedQueue(root/'queue.db');q.close();self.assertTrue(check(root/'shared.db',root/'queue.db'))
  before=self.store.shared.ledger.balance('spot:free','USDT');app=DonchianSpotApplication(self.store,PaperExchange(),self.reader,clock=self.clock)
  with self.store.transaction():self.store.set('paused',False)
  self.clock.t=self.b/1000+1
  with patch('trading_bot.runtime_store.time.time',self.clock):app.tick()
  self.assertTrue(self.store.shared.ledger.reconcile()['balanced']);self.assertLess(self.store.shared.ledger.balance('spot:free','USDT'),before)
  with self.assertRaises(ValueError):check(root/'shared.db',root/'queue.db')
  self.reader.boundary+=DAY_MS;self.reader.breakout=False;self.clock.t=self.reader.boundary/1000+1
  with patch('trading_bot.runtime_store.time.time',self.clock):app.tick()
  self.assertTrue(self.store.shared.ledger.reconcile()['balanced']);self.assertEqual(self.store.shared.ledger.balance('spot:position','BTC'),0);self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],2)
 def test_runner_selects_daily_strategy_not_sma(self):
  root=Path(self.tmp.name);self.store.close();base=RuntimeStore(root/'base.db',{'mode':'paper','symbol':'BTCUSDT','interval':'15m','amount':'10'});base.close();f=FuturesStore(root/'futures.db');f.close();consolidate_snapshots(root/'base.db',root/'futures.db',root/'shared.db',captured_at=self.clock())
  store=RuntimeStore(root/'shared.db',{'mode':'paper','symbol':'BTCUSDT','interval':'15m','amount':'10'},shared=True);queue=UnifiedQueue(root/'queue.db',allow_pair_experiment=True,donchian_spot=True)
  try:
   runner=ExperimentalSpotRunner(store,queue,self.reader,donchian=True);self.assertIsInstance(runner.app,DonchianSpotApplication)
   queue.receive(1,True,'/pair ETHUSDT',fresh=True);self.assertEqual(queue.db.execute('select count(*) from unified_jobs').fetchone()[0],0)
  finally:queue.close();store.close()
