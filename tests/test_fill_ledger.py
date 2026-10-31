import os, subprocess, sys, tempfile, unittest
from fractions import Fraction
from pathlib import Path
from trading_bot.order_journal import OrderJournal, Request, OfflineOrderCoordinator, DispatchBlocked
from trading_bot.order_simulator import SimulatedExchange
from trading_bot.fill_ledger import Fill, SimulatedFills, initialize, snapshot, reconcile_fills

class LedgerTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name); self.path=self.root/'j.db'; self.sim=SimulatedExchange(self.root/'s.db')
  with OrderJournal(self.path) as j:
   initialize(j,'1000'); self.buy=j.prepare('test','buy',Request('BUY',quote_quantity='100')); self.buy=OfflineOrderCoordinator(j,self.sim).dispatch(self.buy.intent_id)
 def fill(self,intent,trade,seq,quantity,quote,fee_asset='USDT',fee='0'):
  return Fill(trade,intent.exchange_order_id,intent.client_order_id,seq,intent.request.side,quantity,quote,fee_asset,fee)
 def apply(self,intent,fills):
  with OrderJournal(self.path) as j:return reconcile_fills(j,SimulatedFills(fills),intent.intent_id)
 def state(self):
  with OrderJournal(self.path) as j:return snapshot(j)
 def test_partial_buy_sell_fees_dust_duplicate(self):
  a=self.fill(self.buy,'1',1,'0.001','50','BTC','0.000001'); b=self.fill(self.buy,'2',2,'0.001','50','USDT','0.05')
  self.assertEqual(self.apply(self.buy,[a,b]),'recorded'); before=self.state(); self.apply(self.buy,[a,b]); self.assertEqual(self.state(),before)
  with OrderJournal(self.path) as j:
   sell=j.prepare('test','sell',Request('SELL',quantity='0.001')); sell=OfflineOrderCoordinator(j,self.sim).dispatch(sell.intent_id)
  c=self.fill(sell,'3',3,'0.001','60','USDT','0.06'); self.assertEqual(self.apply(sell,[c]),'recorded')
  s=self.state(); self.assertEqual(Fraction(s['base_balance']),Fraction('0.000999')); self.assertEqual(Fraction(s['quote_balance']),Fraction('959.89'))
  self.assertEqual(Fraction(s['realized_pnl_excluding_unknown_fees']),Fraction('59.94')-Fraction('100.05')*Fraction('0.001')/Fraction('0.001999'))
 def test_unknown_fee_and_conflict_durable_gate(self):
  a=self.fill(self.buy,'1',1,'0.001','50','BNB','0.01'); self.apply(self.buy,[a]); self.assertIsNone(self.state()['net_pnl'])
  bad=self.fill(self.buy,'1',1,'0.001','51','BNB','0.01'); self.assertEqual(self.apply(self.buy,[bad]),'contradiction'); self.assertTrue(self.state()['blocked'])
  with OrderJournal(self.path) as j:
   other=j.prepare('test','other',Request('BUY',quote_quantity='10'))
   with self.assertRaises(DispatchBlocked):OfflineOrderCoordinator(j,self.sim).dispatch(other.intent_id)
 def test_overfill_rolls_back_entire_batch(self):
  a=self.fill(self.buy,'1',1,'0.001','50'); b=self.fill(self.buy,'2',2,'0.002','60')
  self.assertEqual(self.apply(self.buy,[a,b]),'contradiction')
  state=self.state(); self.assertTrue(state['blocked']); self.assertEqual(Fraction(state['base_balance']),0); self.assertEqual(Fraction(state['quote_balance']),1000)
  with OrderJournal(self.path) as j:self.assertEqual(j.db.execute('SELECT count(*) FROM ledger_fills').fetchone()[0],0)
 def test_fee_in_base_on_partial_exit(self):
  self.apply(self.buy,[self.fill(self.buy,'1',1,'0.002','100')])
  with OrderJournal(self.path) as j:
   sell=j.prepare('test','exit',Request('SELL',quantity='0.001')); sell=OfflineOrderCoordinator(j,self.sim).dispatch(sell.intent_id)
  self.apply(sell,[self.fill(sell,'2',2,'0.0005','30','BTC','0.000001')])
  s=self.state(); self.assertEqual(Fraction(s['base_balance']),Fraction('0.001499')); self.assertEqual(Fraction(s['net_pnl']),Fraction('30')-Fraction('100')*Fraction('0.000501')/Fraction('0.002'))
 def test_hard_crash_before_and_after_commit(self):
  a=self.fill(self.buy,'1',1,'0.001','50')
  worker='''import os,sys\nfrom trading_bot.order_journal import OrderJournal\nfrom trading_bot.fill_ledger import Fill,SimulatedFills,reconcile_fills\npath,intent,oid,cid,point=sys.argv[1:]\nf=Fill("1",oid,cid,1,"BUY","0.001","50","USDT","0")\nwith OrderJournal(path) as j:reconcile_fills(j,SimulatedFills([f]),intent,lambda p:os._exit(73) if p==point else None)\n'''
  for point in ['before_commit','after_commit']:
   p=subprocess.run([sys.executable,'-B','-c',worker,str(self.path),self.buy.intent_id,self.buy.exchange_order_id,self.buy.client_order_id,point]); self.assertEqual(p.returncode,73)
   s=self.state(); self.assertEqual(Fraction(s['base_balance']),Fraction(0) if point=='before_commit' else Fraction('0.001'))
  self.assertEqual(self.apply(self.buy,[a]),'recorded'); self.assertEqual(Fraction(self.state()['base_balance']),Fraction('0.001'))
if __name__=='__main__': unittest.main()
