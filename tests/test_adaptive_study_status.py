import unittest
from decimal import Decimal
from research.adaptive_study_status import snapshot
from tests import test_adaptive_experiment_cycle as fixture

class StudyStatusTest(unittest.TestCase):
 def setUp(self):self.f=fixture.CycleTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
 def view(self,quotes=None):return snapshot(self.f.root/'ai',quotes=quotes or {},now_ms=self.f.now)
 def test_cash_open_unknown_stale_and_realised(self):
  f=self.f;initial=self.view()
  self.assertEqual(Decimal(initial['cash_usdt']),1000);self.assertEqual(initial['open_pnl_usdt'],'0');self.assertTrue(initial['ledger_balanced'])
  self.assertEqual(f.run_cycle('ai',f.apath)['result']['status'],'OPEN')
  live=self.view({'spot':f.markets['spot']['quote']});self.assertEqual(len(live['positions']),1);self.assertTrue(live['exposure_valid'])
  self.assertLess(Decimal(live['open_pnl_usdt']),0)
  self.assertIsNone(self.view()['open_pnl_usdt']);self.assertIsNone(self.view()['net_pnl_usdt'])
  f.now+=1000;f.markets['spot']['quote'].update(price='97',event_ms=f.now,received_ms=f.now)
  self.assertEqual(f.run_cycle('ai',f.private/'missing')['status'],'protection_exit')
  done=self.view();self.assertEqual(done['positions'],[]);self.assertEqual(done['closed_trades'],1)
  self.assertEqual(Decimal(done['net_pnl_usdt']),Decimal(done['realised_pnl_usdt']));self.assertTrue(done['ledger_balanced'])
 def test_short_closed_net_funding_and_fees_reconcile_to_cash(self):
  f=self.f;f.test_short_funding_and_protection_without_ai()
  done=self.view();self.assertEqual(done['closed_trades'],1);self.assertEqual(done['positions'],[])
  self.assertGreater(Decimal(done['funding_usdt']),0);self.assertGreater(Decimal(done['fees_usdt']),0)
  self.assertEqual(Decimal(done['cash_usdt'])-1000,Decimal(done['net_pnl_usdt']))
 def test_conflicting_funding_hides_unconfirmed_financial_results(self):
  import sqlite3
  f=self.f;f.test_short_funding_and_protection_without_ai()
  with sqlite3.connect(f.root/'ai'/'financial.db') as db:
   db.execute("INSERT INTO funding_conflicts VALUES(?,?,?)",(f.now,'conflicting fixture',f.now))
  data=self.view();self.assertTrue(data['funding_conflict'])
  for key in ('funding_usdt','realised_pnl_usdt','open_pnl_usdt','net_pnl_usdt'):self.assertIsNone(data[key])
 def test_operator_cycles_are_excluded_from_strategy_statistics(self):
  from research.adaptive_operator_intent import create_entry,execute_entry,create_exit,execute_exit
  from tests.test_paper_pair_catalog import spot_info
  from trading_bot.futures_store import FuturesStore
  from trading_bot.unified_queue import UnifiedQueue
  f=self.f;p=f.root/'ai';q=UnifiedQueue(p/'queue.db');s=FuturesStore(p/'financial.db',shared=True,fee_bps='10',slippage_bps='5')
  try:
   info=spot_info('BTC');info.update(contractType='PERPETUAL',marginAsset='USDT',marketTakeBound='0.03')
   trade={'symbol':'BTCUSDT','source':'binance_usdm_trade','trade_id':7,'price':'100','event_ms':f.now,'received_ms':f.now}
   mark={'symbol':'BTCUSDT','source':'binance_usdm_mark','price':'100','event_ms':f.now,'received_ms':f.now,'next_due_ms':f.now+28800000}
   job=create_entry(q,test_id='status-test-001',direction='LONG',fixed_ms=f.now)
   opened=execute_entry(s,p/'queue.db',job_id=job['job_id'],trade=trade,mark=mark,filters=info,filters_ms=f.now,next_funding_ms=mark['next_due_ms'],clock=lambda:f.now)
   close=create_exit(q,test_id='status-test-001',position_id=opened['position']['id'],fixed_ms=f.now)
   done=execute_exit(s,p/'queue.db',job_id=close['job_id'],trade=trade,mark=mark,filters=info,filters_ms=f.now,clock=lambda:f.now)
   view=self.view();self.assertEqual(view['closed_trades'],0);self.assertEqual(Decimal(view['net_pnl_usdt']),0)
   self.assertEqual(len(view['operator_test_closes']),1);self.assertEqual(Decimal(view['cash_usdt'])-1000,Decimal(done['position']['net']))
  finally:q.close();s.close()
 def test_supervised_two_cycles_and_strategy_statistics(self):
  from research.adaptive_operator_worker import supervised
  from tests.test_paper_pair_catalog import spot_info
  f=self.f;info=spot_info('BTC');info.update(contractType='PERPETUAL',marginAsset='USDT',marketTakeBound='0.03')
  class Feed:
   @staticmethod
   def futures_quote():return {'symbol':'BTCUSDT','source':'binance_usdm_mark','price':'100','event_ms':f.now,'received_ms':f.now,'next_due_ms':f.now+28800000}
   @staticmethod
   def futures_execution_trade(*,not_before_ms):return {'symbol':'BTCUSDT','source':'binance_usdm_trade','trade_id':7,'price':'100','event_ms':f.now,'received_ms':f.now}
   @staticmethod
   def filters(market):return {'filters':info,'received_ms':f.now}
  outcome=supervised(f.root,prefix='operator-sequence-001',clock=lambda:f.now,sleeper=lambda _:None,provider=Feed)
  self.assertEqual(outcome['status'],'COMPLETE')
  data=snapshot(f.root/'baseline',quotes={},now_ms=f.now)
  self.assertEqual(data['positions'],[]);self.assertEqual(data['closed_trades'],0)
  self.assertEqual(len(data['operator_test_closes']),2);self.assertEqual(Decimal(data['net_pnl_usdt']),0)
  self.assertTrue(data['ledger_balanced'])
  from research.adaptive_sandbox_backup import backup,verify
  p=f.root/'baseline';destination=p/'operator-backup'
  backup(p/'financial.db',p/'queue.db',p/'observations.db',p/'route.db',destination)
  verify(destination)
  import sqlite3
  with sqlite3.connect(destination/'financial.db') as db:
   self.assertEqual(db.execute("SELECT COUNT(*) FROM futures_events WHERE event='CLOSE'").fetchone()[0],2)
   self.assertEqual(db.execute("SELECT COUNT(*) FROM adaptive_operator_receipts").fetchone()[0],4)
