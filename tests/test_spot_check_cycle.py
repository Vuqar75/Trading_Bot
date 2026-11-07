import json,os,tempfile,time,unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from trading_bot.app import Application
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue
from trading_bot.spot_check_cycle import step
from tests.test_runtime import symbol_info

class Reader:
 def __init__(self,now):self.now=now
 def get(self,path,params=None):
  if path=='/api/v3/time':return {'serverTime':int(self.now*1000)}
  if path=='/api/v3/trades':return [{'id':1,'time':int(self.now*1000)-100,'price':'100'}]
  if path=='/api/v3/exchangeInfo':return {'symbols':[symbol_info()]}
  raise AssertionError('private or unexpected endpoint')

class CheckCycleTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name);self.root=root;self.now=time.time()
  self.store=RuntimeStore(root/'financial',{'mode':'paper'});self.addCleanup(self.store.close)
  self.q=UnifiedQueue(root/'queue');self.addCleanup(self.q.close)
  self.app=Application(self.store,PaperExchange(),Reader(self.now));self.store.command(1,True,'/resume')
  self.path=root/'request.json';self.path.write_text(json.dumps({'schema':1,'id':'one','expires':self.now+300}));self.path.chmod(0o600)
 def cycle(self):return step(self.app,self.q,self.path,clock=lambda:self.now,owner_uid=os.getuid())
 def test_buy_restart_sell_and_replay(self):
  self.assertTrue(self.cycle());first=self.store.get('spot_test_cycle');self.assertEqual(first['phase'],'BUY_FILLED');self.assertTrue(self.store.get('paused'))
  # A real application restart preserves the owned position and never repeats BUY.
  self.app=Application(self.store,PaperExchange(),Reader(self.now));self.assertTrue(self.cycle())
  result=self.store.get('spot_test_cycle');self.assertEqual(result['phase'],'COMPLETE');self.assertEqual(Decimal(self.store.get('position')['quantity']),0)
  self.assertFalse(self.cycle());self.assertEqual(self.store.db.execute('select count(*) from runtime_orders').fetchone()[0],2)
  self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],2);self.assertTrue(self.store.get('paused'))
  self.assertLess(Decimal(result['net_pnl']),0)
 def test_existing_position_or_pending_action_blocks_without_buy(self):
  self.store.prepare('BUY','.1',Decimal('100'),'old','TEST')
  with self.assertRaisesRegex(Exception,'requires review'):self.cycle()
  self.assertEqual(self.store.db.execute("select count(*) from runtime_orders where reason='SPOT_CHECK_BUY'").fetchone()[0],0)
 def test_untrusted_request_never_arms(self):
  self.path.chmod(0o666)
  with self.assertRaisesRegex(ValueError,'untrusted'):self.cycle()
  self.assertIsNone(self.store.get('spot_test_cycle'))
 def test_crash_before_claim_allows_only_proven_unsent_retry(self):
  def prepare_then_crash(side,quantity,price,key,reason,target=None,quote_at=None):
   self.store.prepare(side,quantity,price,key,reason,target=target,quote_at=quote_at)
   raise RuntimeError('injected crash before claim')
  with patch.object(self.app,'execute',side_effect=prepare_then_crash):
   with self.assertRaisesRegex(RuntimeError,'before claim'):self.cycle()
  self.cycle();self.cycle()
  self.assertEqual(self.store.get('spot_test_cycle')['phase'],'BUY_FILLED')
  self.cycle();self.assertEqual(self.store.get('spot_test_cycle')['phase'],'COMPLETE')
  self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],2)
  self.assertEqual(self.store.db.execute("select count(*) from runtime_orders where status='EXPIRED'").fetchone()[0],1)
 def test_sell_before_claim_recovery_preserves_owned_position(self):
  self.cycle();self.assertEqual(self.store.get('spot_test_cycle')['phase'],'BUY_FILLED')
  def prepare_then_crash(side,quantity,price,key,reason,target=None,quote_at=None):
   self.store.prepare(side,quantity,price,key,reason,target=target,quote_at=quote_at)
   raise RuntimeError('injected crash before claim')
  with patch.object(self.app,'execute',side_effect=prepare_then_crash):
   with self.assertRaisesRegex(RuntimeError,'before claim'):self.cycle()
  self.cycle();self.assertEqual(self.store.get('spot_test_cycle')['phase'],'BUY_FILLED')
  self.cycle();self.assertEqual(self.store.get('spot_test_cycle')['phase'],'COMPLETE')
  self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],2)
 def test_unknown_claim_reconciles_without_second_submission(self):
  with patch.object(self.app.exchange,'submit',side_effect=RuntimeError('injected after claim')) as submit:
   self.cycle();self.assertEqual(submit.call_count,1)
   self.assertEqual(self.store.db.execute('select phase from runtime_orders').fetchone()[0],'UNKNOWN')
  self.cycle();self.assertEqual(self.store.get('spot_test_cycle')['phase'],'COMPLETE')
  self.assertEqual(self.store.db.execute('select count(*) from runtime_orders').fetchone()[0],2)
  self.assertEqual(self.store.db.execute('select count(*) from runtime_fills').fetchone()[0],2)
