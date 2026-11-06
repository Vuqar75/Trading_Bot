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
