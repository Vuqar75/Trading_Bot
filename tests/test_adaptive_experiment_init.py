import json,tempfile,unittest
from decimal import Decimal
from pathlib import Path
from research.adaptive_experiment_init import initialize
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore
from trading_bot.futures_store import FuturesStore

class ExperimentInitTest(unittest.TestCase):
 def setUp(self):self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
 def test_independent_single_capital_and_idempotent_restart(self):
  ai=initialize(self.root/'ai',source='AI',start_ms=1000)
  base=initialize(self.root/'baseline',source='baseline',start_ms=1000)
  self.assertFalse(ai['existing']);self.assertFalse(base['existing'])
  for name in ('ai','baseline'):
   shared=SharedPaperStore(self.root/name/'financial.db')
   try:
    self.assertEqual(shared.ledger.balance('spot:free','USDT'),Decimal('500'))
    self.assertEqual(shared.ledger.balance('futures_isolated:free','USDT'),Decimal('500'))
    self.assertTrue(shared.ledger.reconcile()['balanced'])
   finally:shared.close()
  again=initialize(self.root/'ai',source='AI',start_ms=9999)
  self.assertTrue(again['existing']);self.assertEqual(again['start_ms'],1000)
  with self.assertRaisesRegex(ValueError,'identity_conflict'):initialize(self.root/'ai',source='baseline',start_ms=1000)
 def test_restart_preserves_internal_transfer(self):
  initialize(self.root/'ai',source='AI',start_ms=1000)
  spot=RuntimeStore(self.root/'ai'/'financial.db',{'mode':'paper','symbol':'BTCUSDT'},shared=True)
  future=FuturesStore(self.root/'ai'/'financial.db',shared=True,fee_bps='10',slippage_bps='5')
  try:spot.command(2,True,'/pause');future.command(2,True,'/pause')
  finally:spot.close();future.close()
  shared=SharedPaperStore(self.root/'ai'/'financial.db')
  try:
   proposal=shared.propose_transfer('rebalance','spot','futures_isolated','10',now=10)
   shared.confirm_transfer(proposal['id'],now=10)
  finally:shared.close()
  initialize(self.root/'ai',source='AI',start_ms=2000)
  shared=SharedPaperStore(self.root/'ai'/'financial.db')
  try:
   self.assertEqual(shared.ledger.balance('spot:free','USDT'),Decimal('490'))
   self.assertEqual(shared.ledger.balance('futures_isolated:free','USDT'),Decimal('510'))
   self.assertTrue(shared.ledger.reconcile()['balanced'])
  finally:shared.close()
 def test_interrupted_initialization_never_reseeds(self):
  def crash(point):
   if point=='after_financial':raise RuntimeError('crash')
  with self.assertRaises(RuntimeError):initialize(self.root/'ai',source='AI',start_ms=1000,fault=crash)
  self.assertTrue((self.root/'ai'/'financial.db').exists())
  with self.assertRaisesRegex(ValueError,'incomplete_initialization_requires_review'):initialize(self.root/'ai',source='AI',start_ms=2000)
