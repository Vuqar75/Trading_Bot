import os, subprocess, sys, tempfile, unittest
from contextlib import closing
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch
from trading_bot.order_journal import OrderJournal, Request, OfflineOrderCoordinator, State, SubmissionUncertain, DispatchBlocked
from trading_bot.fill_ledger import Fill, SimulatedFills, initialize, reconcile_fills, snapshot
from trading_bot.order_simulator import SimulatedExchange
from trading_bot.order_recovery import RecoveryExchange
from trading_bot.telegram_inbox import ResponseSimulator, DeliveryUncertain
from trading_bot.offline_integration import OfflineIntegration

class GuardedSubmit(SimulatedExchange):
 def submit(self,intent):
  assert not self.integration.journal.db.in_transaction
  assert not self.integration.inbox.db.in_transaction
  return super().submit(intent)
class GuardedRecovery(RecoveryExchange):
 def check(self):
  assert not self.integration.journal.db.in_transaction
  assert not self.integration.inbox.db.in_transaction
 def get_manifest(self):self.check();return super().get_manifest()
 def get_page(self,index):self.check();return super().get_page(index)
 def query(self,client):self.check();return super().query(client)
 def clients(self):self.check();return super().clients()
class GuardedResponse(ResponseSimulator):
 def send(self,update_id,result):
  assert not self.integration.journal.db.in_transaction
  assert not self.integration.inbox.db.in_transaction
  return super().send(update_id,result)

class IntegrationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.idb=self.root/'inbox.db';self.odb=self.root/'orders.db';self.edb=self.root/'exchange.db';self.rdb=self.root/'responses.db'
  self.sim=SimulatedExchange(self.edb)
  with OrderJournal(self.odb) as j:
   initialize(j,'1000');i=j.prepare('fixture','entry',Request('BUY',quantity='0.001')); self.entry=OfflineOrderCoordinator(j,self.sim).dispatch(i.intent_id)
   self.entry_fill=Fill('entry-fill',self.entry.exchange_order_id,self.entry.client_order_id,1,'BUY','0.001','50','USDT','0')
   self.assertEqual(reconcile_fills(j,SimulatedFills([self.entry_fill]),self.entry.intent_id),'recorded')
 def exchange(self,engine,close=None):
  first=self.sim.query(self.entry.client_order_id);first['status']='FILLED';orders={self.entry.client_order_id:first};fills=[self.entry_fill]
  quote,base='950','0.001'
  if close:
   obs=self.sim.query(close.client_order_id);obs['status']='PARTIALLY_FILLED';orders[close.client_order_id]=obs
   fills.append(Fill('exit-fill',obs['exchange_order_id'],close.client_order_id,2,'SELL','0.0004','20','BNB','0.01'))
   quote,base='970','0.0006'
  ex=GuardedRecovery('snapshot',fills,orders,quote,base,page_size=1);ex.integration=engine;return ex
 def test_complete_path_crash_recovery_and_single_accounting(self):
  with patch('socket.socket',side_effect=AssertionError('network')),patch('socket.create_connection',side_effect=AssertionError('network')):
   with OfflineIntegration(self.idb,self.odb,owner='owner-fixture') as app:
    app.inbox.set_simulated_position('fixture-position',1)
    app.receive(1,'intruder','CLOSE','fixture-position',1)
    self.assertIsNone(app.process_close())
    app.receive(2,'owner-fixture','CLOSE','fixture-position',1)
    new=app.journal.prepare('fixture','other-entry',Request('BUY',quote_quantity='1'))
    blocked=GuardedSubmit(self.edb);blocked.integration=app
    with self.assertRaises(DispatchBlocked):app.dispatch(new.intent_id,blocked)
    self.assertEqual(app.recover(self.exchange(app)),'complete')
    self.assertEqual(app.process_close(),2);intent=app.close_intent('fixture-position',1)
    sim=GuardedSubmit(self.edb,scenario='timeout_after_accept');sim.integration=app
    with self.assertRaises(SubmissionUncertain):app.dispatch(intent.intent_id,sim)
    self.assertEqual(app.journal.get(intent.intent_id).state,State.UNKNOWN)
   with OfflineIntegration(self.idb,self.odb,owner='owner-fixture') as app:
    s=GuardedSubmit(self.edb);s.integration=app
    with self.assertRaises(DispatchBlocked):app.dispatch(new.intent_id,s)
    self.assertEqual(app.recover(self.exchange(app,intent)),'complete')
    self.assertEqual(app.recover(self.exchange(app,intent)),'complete')
    self.assertEqual(app.journal.get(intent.intent_id).state,State.ACKNOWLEDGED)
    st=snapshot(app.journal);self.assertEqual(Fraction(st['base_balance']),Fraction('0.0006'))
    self.assertEqual(st['pending_fees'],1);self.assertIsNone(st['net_pnl'])
    self.assertEqual(app.journal.db.execute('SELECT COUNT(*) FROM ledger_fills').fetchone()[0],2)
    self.assertEqual(self.sim.counts()['submissions'],2)
    app.receive(2,'owner-fixture','CLOSE','fixture-position',1);self.assertIsNone(app.process_close())
    response=GuardedResponse(self.rdb,'timeout_after');response.integration=app
    with self.assertRaises(DeliveryUncertain):app.inbox.send_one(response)
    self.assertEqual(app.inbox.db.execute("SELECT status FROM outbox").fetchone()[0],'SENDING')
   with OfflineIntegration(self.idb,self.odb,owner='owner-fixture') as app:
    self.assertEqual(app.recover(self.exchange(app,intent)),'complete')
    app.inbox.requeue_uncertain()
    response=GuardedResponse(self.rdb);response.integration=app
    self.assertEqual(app.inbox.send_one(response),2)
    self.assertEqual(app.inbox.db.execute("SELECT COUNT(*) FROM outbox WHERE status='SENT'").fetchone()[0],1)
    self.assertEqual(app.journal.db.execute('SELECT COUNT(*) FROM ledger_fills').fetchone()[0],2)
    self.assertEqual(self.sim.counts()['submissions'],2)
 def test_hard_crash_after_exchange_acceptance(self):
  with OfflineIntegration(self.idb,self.odb,owner='owner-fixture') as app:
   app.inbox.set_simulated_position('p',1);app.receive(1,'owner-fixture','CLOSE','p',1);app.process_close();order=app.close_intent('p',1)
  script='''import os,sys\nfrom trading_bot.offline_integration import OfflineIntegration\nfrom trading_bot.order_simulator import SimulatedExchange\nfrom trading_bot.order_recovery import RecoveryExchange\nfrom trading_bot.fill_ledger import Fill\ni,o,e,ec,eo,close=sys.argv[1:]\na=Fill("entry-fill",eo,ec,1,"BUY","0.001","50","USDT","0")\nsim=SimulatedExchange(e)\nobs=sim.query(ec);obs["status"]="FILLED"\nwith OfflineIntegration(i,o,owner="owner-fixture") as app:\n assert app.recover(RecoveryExchange("pre",[a],{ec:obs},"950","0.001"))=="complete"\n def die(p):\n  if p=="simulator_after_submit":os._exit(88)\n app.dispatch(close,SimulatedExchange(e,fault=die))\n'''
  p=subprocess.run([sys.executable,'-B','-c',script,str(self.idb),str(self.odb),str(self.edb),self.entry.client_order_id,self.entry.exchange_order_id,order.intent_id])
  self.assertEqual(p.returncode,88)
  with OfflineIntegration(self.idb,self.odb,owner='owner-fixture') as app:
   self.assertEqual(app.journal.get(order.intent_id).state,State.SUBMITTING)
   self.assertEqual(app.recover(self.exchange(app,order)),'complete')
   self.assertEqual(app.journal.db.execute('SELECT COUNT(*) FROM ledger_fills').fetchone()[0],2)
   guarded=GuardedSubmit(self.edb);guarded.integration=app
   with self.assertRaises(DispatchBlocked):app.dispatch(order.intent_id,guarded)
   self.assertEqual(self.sim.counts()['submissions'],2)
 def test_old_close_cannot_close_new_version(self):
  with OfflineIntegration(self.idb,self.odb,owner='owner-fixture') as app:
   app.inbox.set_simulated_position('p',1);app.receive(1,'owner-fixture','CLOSE','p',1)
   app.inbox.set_simulated_position('p',2);app.process_close()
   self.assertIsNone(app.close_intent('p',1))
   self.assertEqual(app.inbox.db.execute('SELECT result FROM outbox').fetchone()[0],'STALE_POSITION')
 def test_two_close_commands_one_order_intent(self):
  with OfflineIntegration(self.idb,self.odb,owner='owner-fixture') as app:
   app.inbox.set_simulated_position('p',1);app.receive(1,'owner-fixture','CLOSE','p',1);app.receive(2,'owner-fixture','CLOSE','p',1)
   app.process_close();app.process_close()
   self.assertEqual(app.journal.db.execute("SELECT count(*) FROM order_intents WHERE namespace='synthetic-close'").fetchone()[0],1)
   self.assertEqual([r[0] for r in app.inbox.db.execute('SELECT result FROM outbox ORDER BY update_id')],['CLOSE_INTENT_CREATED','CLOSE_ALREADY_PENDING'])
 def test_command_hard_crash_across_both_databases(self):
  script='''import os,sys\nfrom trading_bot.offline_integration import OfflineIntegration\na,b,point=sys.argv[1:]\ndef fault(p):\n if p==point:os._exit(87)\nwith OfflineIntegration(a,b,owner="owner-fixture",fault=fault) as app:app.process_close()\n'''
  for index,point in enumerate(['intent_written','command_before_commit','command_committed']):
   inbox=self.root/(str(index)+'.db')
   with OfflineIntegration(inbox,self.odb,owner='owner-fixture') as app:
    app.inbox.set_simulated_position('p'+str(index),1);app.receive(index+10,'owner-fixture','CLOSE','p'+str(index),1)
   p=subprocess.run([sys.executable,'-B','-c',script,str(inbox),str(self.odb),point]);self.assertEqual(p.returncode,87)
   with OfflineIntegration(inbox,self.odb,owner='owner-fixture') as app:
    count=app.journal.db.execute("SELECT count(*) FROM order_intents WHERE namespace='synthetic-close' AND business_key=?",('close-p'+str(index)+'-1',)).fetchone()[0]
    self.assertEqual(count,1 if point=='command_committed' else 0)
    self.assertEqual(app.inbox.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],count)
    app.process_close(); app.process_close()
    self.assertEqual(app.journal.db.execute("SELECT count(*) FROM order_intents WHERE namespace='synthetic-close' AND business_key=?",('close-p'+str(index)+'-1',)).fetchone()[0],1)
if __name__=='__main__':unittest.main()
