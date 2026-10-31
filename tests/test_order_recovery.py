import os, sys, subprocess, tempfile, unittest
from pathlib import Path
from fractions import Fraction
from trading_bot.fill_ledger import Fill, SimulatedFills, initialize, snapshot, reconcile_fills
from trading_bot.order_journal import OrderJournal, Request, OfflineOrderCoordinator, DispatchBlocked, SubmissionUncertain, State
from trading_bot.order_simulator import SimulatedExchange
from trading_bot.order_recovery import RecoveryExchange, RecoverySession, recover

class RecoveryTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); root=Path(self.tmp.name); self.db=root/'journal.db'; self.sim=SimulatedExchange(root/'sim.db')
  with OrderJournal(self.db) as j:
   initialize(j,'1000'); i=j.prepare('test','buy',Request('BUY',quote_quantity='100')); self.intent=OfflineOrderCoordinator(j,self.sim).dispatch(i.intent_id)
  self.fill=Fill('trade1',self.intent.exchange_order_id,self.intent.client_order_id,1,'BUY','0.001','50','BNB','0.01')
  self.order=self.sim.query(self.intent.client_order_id); self.order['status']='PARTIALLY_FILLED'
 def exchange(self):return RecoveryExchange('stable',[self.fill],{self.intent.client_order_id:self.order},'950','0.001',1)
 def test_unknown_submit_recovered_after_restart(self):
  db=Path(self.tmp.name)/'unknown.db'; sim=SimulatedExchange(Path(self.tmp.name)/'uncertain.db',scenario='timeout_after_accept')
  with OrderJournal(db) as j:
   initialize(j,'1000'); order=j.prepare('test','unknown',Request('BUY',quote_quantity='100'))
   with self.assertRaises(SubmissionUncertain):OfflineOrderCoordinator(j,sim).dispatch(order.intent_id)
  obs=sim.query(order.client_order_id); obs['status']='PARTIALLY_FILLED'
  fill=Fill('u1',obs['exchange_order_id'],order.client_order_id,1,'BUY','0.001','50','USDT','0')
  ex=RecoveryExchange('u',[fill],{order.client_order_id:obs},'950','0.001')
  with RecoverySession(db) as session:
   self.assertEqual(session.tick(ex),'complete')
   self.assertEqual(session.journal.get(order.intent_id).state,State.ACKNOWLEDGED)
   with self.assertRaises(DispatchBlocked):OfflineOrderCoordinator(session.journal,sim).dispatch(order.intent_id)
  self.assertEqual(sim.counts()['submissions'],1)
 def test_repeat_and_unknown_fee(self):
  with OrderJournal(self.db) as j:
   self.assertEqual(recover(j,self.exchange()),'complete'); first=snapshot(j)
   self.assertFalse(first['blocked']); self.assertIsNone(first['net_pnl']); self.assertEqual(first['pending_fees'],1)
  with OrderJournal(self.db) as j:
   self.assertEqual(recover(j,self.exchange()),'complete'); self.assertEqual(snapshot(j),first)
   self.assertEqual(j.db.execute('SELECT COUNT(*) FROM ledger_fills').fetchone()[0],1)
 def test_missing_page_stays_blocked_after_restart(self):
  ex=self.exchange(); ex.get_page=lambda i: (_ for _ in ()).throw(TimeoutError())
  with OrderJournal(self.db) as j:self.assertEqual(recover(j,ex),'incomplete')
  with OrderJournal(self.db) as j:
   self.assertEqual(snapshot(j)['reason'],'RECOVERY_INCOMPLETE')
   other=j.prepare('test','other',Request('BUY',quote_quantity='1'))
   with self.assertRaises(DispatchBlocked): OfflineOrderCoordinator(j,self.sim).dispatch(other.intent_id)
   self.assertEqual(recover(j,self.exchange()),'complete')
 def test_inconsistent_balance_is_durable_contradiction(self):
  ex=self.exchange(); ex.base_balance=Fraction('0')
  with OrderJournal(self.db) as j:
   self.assertEqual(recover(j,ex),'contradiction'); self.assertEqual(snapshot(j)['reason'],'CONTRADICTION')
  with OrderJournal(self.db) as j:self.assertEqual(recover(j,self.exchange()),'contradiction')
 def test_late_earlier_fill_replays_in_order(self):
  later=Fill('trade2',self.intent.exchange_order_id,self.intent.client_order_id,2,'BUY','0.001','40','USDT','0')
  earlier=Fill('trade1',self.intent.exchange_order_id,self.intent.client_order_id,1,'BUY','0.001','50','BNB','0.01')
  with OrderJournal(self.db) as j:
   self.assertEqual(reconcile_fills(j,SimulatedFills([later]),self.intent.intent_id),'recorded')
   ex=RecoveryExchange('later',[earlier,later],{self.intent.client_order_id:self.order},'910','0.002',1)
   self.assertEqual(recover(j,ex),'complete')
   self.assertEqual(Fraction(snapshot(j)['base_balance']),Fraction('0.002'))
   self.assertIsNone(snapshot(j)['net_pnl'])
   self.assertEqual(j.db.execute('SELECT count(*) FROM ledger_fills').fetchone()[0],2)
 def test_duplicate_and_out_of_order_page_delivery(self):
  a=Fill('trade1',self.intent.exchange_order_id,self.intent.client_order_id,1,'BUY','0.0005','25','USDT','0')
  b=Fill('trade2',self.intent.exchange_order_id,self.intent.client_order_id,2,'BUY','0.0005','25','USDT','0')
  ex=RecoveryExchange('paged',[a,b],{self.intent.client_order_id:self.order},'950','0.001',1)
  original=ex.get_page; calls=[0]
  def page(index):
   calls[0]+=1
   if index==0 and calls[0]==1:return original(1)
   if index==0 and calls[0]==2:return original(1)
   return original(index)
  ex.get_page=page
  with OrderJournal(self.db) as j:
   self.assertEqual(recover(j,ex),'complete')
   self.assertEqual(j.db.execute('SELECT COUNT(*) FROM ledger_fills').fetchone()[0],2)
 def test_snapshot_change_during_fetch_keeps_gate(self):
  ex=self.exchange(); old=ex.get_page
  def page(index):
   p=old(index); ex.quote_balance=Fraction('999'); return p
  ex.get_page=page
  with OrderJournal(self.db) as j:
   self.assertEqual(recover(j,ex),'incomplete')
   self.assertEqual(snapshot(j)['reason'],'RECOVERY_INCOMPLETE')
 def test_corrupt_cached_pnl_blocks(self):
  with OrderJournal(self.db) as j:
   j.db.execute("UPDATE ledger_account SET pnl='7'")
   self.assertEqual(recover(j,self.exchange()),'contradiction')
   self.assertEqual(snapshot(j)['reason'],'CONTRADICTION')
 def test_periodic_session_requires_startup_recovery(self):
  now=[0]
  with RecoverySession(self.db,10,clock=lambda:now[0]) as session:
   self.assertTrue(snapshot(session.journal)['blocked'])
   self.assertEqual(session.tick(self.exchange()),'complete')
   self.assertEqual(session.tick(self.exchange()),'not_due')
   now[0]=10; self.assertEqual(session.tick(self.exchange()),'complete')
  with RecoverySession(self.db,10,clock=lambda:now[0]) as session:
   self.assertTrue(snapshot(session.journal)['blocked'])
   self.assertEqual(session.tick(self.exchange()),'complete')
 def test_order_status_progression_and_regression(self):
  ex=self.exchange(); ex._orders[self.intent.client_order_id]['status']='PARTIALLY_FILLED'
  with OrderJournal(self.db) as j:
   self.assertEqual(recover(j,ex),'complete')
   self.assertEqual(j.get(self.intent.intent_id).exchange_status,'PARTIALLY_FILLED')
   regression=self.exchange(); regression._orders[self.intent.client_order_id]['status']='NEW'
   self.assertEqual(recover(j,regression),'contradiction')
   self.assertTrue(snapshot(j)['blocked'])
 def test_process_crash_between_two_pages(self):
  script='''import os,sys\nfrom trading_bot.order_journal import OrderJournal\nfrom trading_bot.fill_ledger import Fill\nfrom trading_bot.order_recovery import RecoveryExchange,recover\npath,cid,oid=sys.argv[1:]\na=Fill("a",oid,cid,1,"BUY","0.0005","25","USDT","0")\nb=Fill("b",oid,cid,2,"BUY","0.0005","25","USDT","0")\no={"client_order_id":cid,"exchange_order_id":oid,"symbol":"BTCUSDT","side":"BUY","quantity":None,"quote_quantity":"100","status":"PARTIALLY_FILLED"}\nwith OrderJournal(path) as j:recover(j,RecoveryExchange("pages",[a,b],{cid:o},"950","0.001",1),lambda p:os._exit(76) if p=="page_received" else None)\n'''
  p=subprocess.run([sys.executable,'-B','-c',script,str(self.db),self.intent.client_order_id,self.intent.exchange_order_id]); self.assertEqual(p.returncode,76)
  a=Fill('a',self.intent.exchange_order_id,self.intent.client_order_id,1,'BUY','0.0005','25','USDT','0')
  b=Fill('b',self.intent.exchange_order_id,self.intent.client_order_id,2,'BUY','0.0005','25','USDT','0')
  ex=RecoveryExchange('pages',[a,b],{self.intent.client_order_id:self.order},'950','0.001',1)
  with OrderJournal(self.db) as j:
   self.assertEqual(snapshot(j)['reason'],'RECOVERY_INCOMPLETE')
   self.assertEqual(j.db.execute('SELECT COUNT(*) FROM ledger_fills').fetchone()[0],0)
   self.assertEqual(recover(j,ex),'complete')
   self.assertEqual(j.db.execute('SELECT COUNT(*) FROM ledger_fills').fetchone()[0],2)
 def test_crash_before_and_inside_and_after_commit(self):
  script='''import os,sys\nfrom trading_bot.order_journal import OrderJournal\nfrom trading_bot.fill_ledger import Fill\nfrom trading_bot.order_recovery import RecoveryExchange,recover\npath,cid,oid,point=sys.argv[1:]\nf=Fill("trade1",oid,cid,1,"BUY","0.001","50","BNB","0.01")\no={"client_order_id":cid,"exchange_order_id":oid,"symbol":"BTCUSDT","side":"BUY","quantity":None,"quote_quantity":"100","status":"PARTIALLY_FILLED"}\nwith OrderJournal(path) as j: recover(j,RecoveryExchange("stable",[f],{cid:o},"950","0.001",1),lambda p:os._exit(75) if p==point else None)\n'''
  for point in ['page_received','before_final_commit','inside_final_commit','after_final_commit']:
   p=subprocess.run([sys.executable,'-B','-c',script,str(self.db),self.intent.client_order_id,self.intent.exchange_order_id,point]); self.assertEqual(p.returncode,75)
   with OrderJournal(self.db) as j:
    state=snapshot(j); self.assertEqual(state['blocked'],point!='after_final_commit')
    self.assertEqual(recover(j,self.exchange()),'complete'); self.assertEqual(j.db.execute('SELECT COUNT(*) FROM ledger_fills').fetchone()[0],1)
if __name__=='__main__':unittest.main()
