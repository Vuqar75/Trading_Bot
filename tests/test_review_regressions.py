import unittest
import test_offline_integration as fixtures
GuardedSubmit=fixtures.GuardedSubmit
from trading_bot.offline_integration import OfflineIntegration
from trading_bot.order_reconciliation import reconcile
from trading_bot.order_journal import OrderJournal, DispatchBlocked
from trading_bot.order_recovery import _mark, recover

def stale(t):
 with OfflineIntegration(t.idb,t.odb,owner='owner-fixture') as a:
  a.recover(t.exchange(a));a.inbox.set_simulated_position('p',1);a.receive(1,'owner-fixture','CLOSE','p',1);a.process_close();i=a.close_intent('p',1)
  a.inbox.set_simulated_position('p',2)
  s=GuardedSubmit(t.edb);s.integration=a
  out=a.dispatch(i.intent_id,s)
  return {'new_position_version':2,'old_close_sent':out.state.value,'submissions':t.sim.counts()['submissions']}
def poison(t):
 with OfflineIntegration(t.idb,t.odb,owner='owner-fixture') as a:
  a.inbox.set_simulated_position('p',1);a.receive(1,'owner-fixture','PAUSE');a.receive(2,'owner-fixture','CLOSE','p',1)
  errors=[]
  for _ in range(2):
   try:a.process_close()
   except ValueError as e:errors.append(str(e))
  return {'errors':errors,'statuses':[tuple(r) for r in a.inbox.db.execute('SELECT update_id,status FROM updates ORDER BY update_id')]}
def regression(t):
 with OrderJournal(t.odb) as a,OrderJournal(t.odb) as b:
  obs=t.sim.query(t.entry.client_order_id)
  class Source:
   is_simulator=True
   def __init__(self,status):self.status=status
   def query(self,c):return dict(obs,status=self.status)
  class Racing(Source):
   def query(self,c):
    reconcile(b,Source('FILLED'),t.entry.intent_id)
    return dict(obs,status='PARTIALLY_FILLED')
  reconcile(a,Racing('PARTIALLY_FILLED'),t.entry.intent_id)
  return {'final_status':a.get(t.entry.intent_id).exchange_status}
def epoch(t):
 with OfflineIntegration(t.idb,t.odb,owner='owner-fixture') as a,OrderJournal(t.odb) as b:
  def fault(p):
   if p=='before_final_commit':_mark(b,'RECOVERY_INCOMPLETE')
  result=recover(a.journal,t.exchange(a),fault)
  return {'result':result,'gate':tuple(b.db.execute('SELECT blocked,reason FROM ledger_control').fetchone())}
def underfill(t):
 with OfflineIntegration(t.idb,t.odb,owner='owner-fixture') as a:
  a.recover(t.exchange(a));a.inbox.set_simulated_position('p',1);a.receive(1,'owner-fixture','CLOSE','p',1);a.process_close();i=a.close_intent('p',1)
  s=GuardedSubmit(t.edb);s.integration=a;a.dispatch(i.intent_id,s)
  ex=t.exchange(a,i);ex._orders[i.client_order_id]['status']='FILLED'
  result=a.recover(ex)
  return {'result':result,'requested_quantity':i.request.quantity,'executed_quantity':'0.0004','stored_status':a.journal.get(i.intent_id).exchange_status,'gate':tuple(a.journal.db.execute('SELECT blocked,reason FROM ledger_control').fetchone())}

class ReviewRegressionTests(unittest.TestCase):
 setUp=fixtures.IntegrationTests.setUp
 exchange=fixtures.IntegrationTests.exchange
 def test_stale_close_is_not_sent(self):
  with self.assertRaises(DispatchBlocked): stale(self)
 def test_queue_does_not_stall_on_pause(self):
  result=poison(self)
  self.assertNotIn((2, 'PENDING'),result['statuses'])
 def test_status_never_regresses(self):
  self.assertEqual(regression(self)['final_status'],'FILLED')
 def test_new_recovery_generation_remains_blocked(self):
  result=epoch(self)
  self.assertNotEqual(result['result'],'complete')
  self.assertEqual(result['gate'][0],1)
 def test_filled_requires_full_quantity(self):
  result=underfill(self)
  self.assertNotEqual(result['result'],'complete')
  self.assertEqual(result['gate'][0],1)

 def test_pause_survives_recovery_and_allows_confirmed_exit(self):
  from trading_bot.order_journal import Request
  with OfflineIntegration(self.idb,self.odb,owner='owner-fixture') as a:
   a.inbox.set_simulated_position('p',1)
   self.assertEqual(a.recover(self.exchange(a)),'complete')
   entry=a.journal.prepare('test','paused-buy',Request('BUY',quote_quantity='1'))
   sim=GuardedSubmit(self.edb);sim.integration=a
   with self.assertRaises(DispatchBlocked):a.dispatch(entry.intent_id,sim)
   a.receive(1,'owner-fixture','CLOSE','p',1);a.process_close()
   self.assertEqual(a.dispatch(a.close_intent('p',1).intent_id,sim).state.value,'ACKNOWLEDGED')
   self.assertEqual(a.inbox.db.execute('SELECT paused FROM inbox_state').fetchone()[0],1)
 def test_uncertain_exit_reservation_survives_restart_and_blocks_another(self):
  from trading_bot.order_journal import SubmissionUncertain
  with OfflineIntegration(self.idb,self.odb,owner='owner-fixture') as a:
   a.recover(self.exchange(a));a.inbox.set_simulated_position('p',1);a.receive(1,'owner-fixture','CLOSE','p',1);a.process_close();i=a.close_intent('p',1)
   sim=GuardedSubmit(self.edb,scenario='timeout_after_accept');sim.integration=a
   with self.assertRaises(SubmissionUncertain):a.dispatch(i.intent_id,sim)
  with OfflineIntegration(self.idb,self.odb,owner='owner-fixture') as a:
   self.assertEqual(a.recover(self.exchange(a,i)),'complete')
   a.inbox.set_simulated_position('p',2);a.receive(2,'owner-fixture','CLOSE','p',2);a.process_close();other=a.close_intent('p',2)
   sim=GuardedSubmit(self.edb);sim.integration=a
   with self.assertRaises(DispatchBlocked):a.dispatch(other.intent_id,sim)
   self.assertEqual(a.journal.db.execute('SELECT active FROM close_reservations WHERE intent_id=?',(i.intent_id,)).fetchone()[0],1)
 def test_new_generation_immediately_after_gate_is_not_lost(self):
  with OfflineIntegration(self.idb,self.odb,owner='owner-fixture') as a,OrderJournal(self.odb) as b:
   def fault(point):
    if point=='gate_committed':_mark(b,'RECOVERY_INCOMPLETE')
   self.assertEqual(recover(a.journal,self.exchange(a),fault),'incomplete')
   self.assertEqual(b.db.execute('SELECT blocked FROM ledger_control').fetchone()[0],1)
 def test_claim_rollback_does_not_leave_reservation(self):
  with OfflineIntegration(self.idb,self.odb,owner='owner-fixture') as a:
   a.recover(self.exchange(a));a.inbox.set_simulated_position('p',1);a.receive(1,'owner-fixture','CLOSE','p',1);a.process_close();i=a.close_intent('p',1)
   def fault(point):
    if point=='claim_before_commit': raise RuntimeError('crash')
   a.fault=fault
   sim=GuardedSubmit(self.edb);sim.integration=a
   with self.assertRaises(RuntimeError):a.dispatch(i.intent_id,sim)
   self.assertEqual(a.journal.get(i.intent_id).state.value,'PREPARED')
   self.assertEqual(a.journal.db.execute('SELECT COUNT(*) FROM close_reservations').fetchone()[0],0)

 def test_release_only_after_complete_filled_recovery(self):
  from trading_bot.fill_ledger import Fill
  with OfflineIntegration(self.idb,self.odb,owner='owner-fixture') as a:
   a.recover(self.exchange(a));a.inbox.set_simulated_position('p',1);a.receive(1,'owner-fixture','CLOSE','p',1);a.process_close();i=a.close_intent('p',1)
   sim=GuardedSubmit(self.edb);sim.integration=a;a.dispatch(i.intent_id,sim)
   ex=self.exchange(a,i);ex._orders[i.client_order_id]['status']='FILLED'
   old=list(ex.pages[0].fills)+list(ex.pages[1].fills)
   old[1]=Fill('exit-fill',a.journal.get(i.intent_id).exchange_order_id,i.client_order_id,2,'SELL','0.001','50','USDT','0')
   from trading_bot.order_recovery import RecoveryExchange
   done=RecoveryExchange('done',old,ex._orders,'1000','0')
   self.assertEqual(a.recover(done),'complete')
   self.assertEqual(a.journal.db.execute('SELECT active FROM close_reservations WHERE intent_id=?',(i.intent_id,)).fetchone()[0],0)
   self.assertEqual(a.inbox.db.execute('SELECT paused FROM inbox_state').fetchone()[0],1)
 def test_claim_real_crash_before_and_after_commit(self):
  import os, subprocess, sys
  for point in ('claim_before_commit','claim_committed'):
   fixture=fixtures.IntegrationTests();fixture.setUp()
   try:
    with OfflineIntegration(fixture.idb,fixture.odb,owner='owner-fixture') as a:
     a.inbox.set_simulated_position('p',1);a.receive(1,'owner-fixture','CLOSE','p',1);a.process_close();i=a.close_intent('p',1)
    script="""import os,sys
from trading_bot.offline_integration import OfflineIntegration
from trading_bot.order_simulator import SimulatedExchange
from trading_bot.order_recovery import RecoveryExchange
from trading_bot.fill_ledger import Fill
ib,ob,eb,client,oid,intent,point=sys.argv[1:]
sim=SimulatedExchange(eb);obs=sim.query(client);obs['status']='FILLED'
f=Fill('entry-fill',oid,client,1,'BUY','0.001','50','USDT','0')
def fault(p):
 if p==point:os._exit(87)
with OfflineIntegration(ib,ob,owner='owner-fixture',fault=fault) as a:
 assert a.recover(RecoveryExchange('pre',[f],{client:obs},'950','0.001'))=='complete'
 a.dispatch(intent,sim)
"""
    run=subprocess.run([sys.executable,'-B','-c',script,str(fixture.idb),str(fixture.odb),str(fixture.edb),fixture.entry.client_order_id,fixture.entry.exchange_order_id,i.intent_id,point])
    self.assertEqual(run.returncode,87)
    with OfflineIntegration(fixture.idb,fixture.odb,owner='owner-fixture') as a:
     self.assertEqual(a.journal.get(i.intent_id).state.value,'PREPARED' if point=='claim_before_commit' else 'SUBMITTING')
     self.assertEqual(a.journal.db.execute('SELECT COUNT(*) FROM close_reservations WHERE active=1').fetchone()[0],0 if point=='claim_before_commit' else 1)
    self.assertEqual(fixture.sim.counts()['submissions'],1)
   finally:fixture.doCleanups()

 def test_filled_quote_budget_without_executed_totals_remains_incomplete(self):
  with OfflineIntegration(self.idb,self.odb,owner='owner-fixture') as a:
   a.journal.db.execute("UPDATE order_intents SET quantity=NULL,quote_quantity='100' WHERE intent_id=?",(self.entry.intent_id,))
   ex=self.exchange(a);ex._orders[self.entry.client_order_id].update(quantity=None,quote_quantity='100',status='FILLED')
   self.assertEqual(a.recover(ex),'incomplete')
   self.assertEqual(a.journal.db.execute('SELECT blocked FROM ledger_control').fetchone()[0],1)
 def test_competing_claims_single_reservation(self):
  import threading
  ready=threading.Barrier(3);go=threading.Event();results=[]
  with OfflineIntegration(self.idb,self.odb,owner='owner-fixture') as main:
   main.inbox.set_simulated_position('p',1);main.receive(1,'owner-fixture','CLOSE','p',1);main.process_close();intent=main.close_intent('p',1)
   def worker():
    with OfflineIntegration(self.idb,self.odb,owner='owner-fixture') as app:
     ready.wait(timeout=10);go.wait(timeout=10)
     try:app._claim(intent.intent_id);results.append('claimed')
     except DispatchBlocked:results.append('blocked')
   threads=[threading.Thread(target=worker) for _ in range(2)]
   for t in threads:t.start()
   ready.wait(timeout=10);self.assertEqual(main.recover(self.exchange(main)),'complete');go.set()
   for t in threads:t.join(timeout=10)
   self.assertCountEqual(results,['claimed','blocked'])
   self.assertEqual(main.journal.db.execute('SELECT COUNT(*) FROM close_reservations WHERE active=1').fetchone()[0],1)

 def test_active_entry_blocks_exit(self):
  from trading_bot.order_journal import Request,OfflineOrderCoordinator
  with OfflineIntegration(self.idb,self.odb,owner='owner-fixture') as a:
   a.recover(self.exchange(a));a.inbox.set_simulated_position('p',1);a.receive(1,'owner-fixture','CLOSE','p',1);a.process_close();i=a.close_intent('p',1)
   pending=a.journal.prepare('test','another-buy',Request('BUY',quantity='0.001'))
   OfflineOrderCoordinator(a.journal,self.sim).dispatch(pending.intent_id)
   sim=GuardedSubmit(self.edb);sim.integration=a
   with self.assertRaises(DispatchBlocked):a.dispatch(i.intent_id,sim)
