import unittest
import test_offline_integration as fixtures
from trading_bot.offline_integration import OfflineIntegration
from trading_bot.order_journal import Request,Receipt
from trading_bot.fill_ledger import Fill
from trading_bot.order_recovery import RecoveryExchange
GuardedSubmit=fixtures.GuardedSubmit

def setup(t,a):
 assert a.recover(t.exchange(a))=='complete'
 a.inbox.set_simulated_position('p',1)
 a.receive(1,'owner-fixture','CLOSE','p',1);a.process_close()
 return a.close_intent('p',1)

def rejected_retry(t):
 with OfflineIntegration(t.idb,t.odb,owner='owner-fixture') as a:
  i=setup(t,a)
  class Reject:
   is_simulator=True
   def submit(self,i):return Receipt(None,'REJECTED')
  a.dispatch(i.intent_id,Reject())
  a.receive(2,'owner-fixture','CLOSE','p',1);a.process_close()
  return {'reservation':tuple(a.journal.db.execute('SELECT active FROM close_reservations').fetchone()),'reply':a.inbox.db.execute('SELECT result FROM outbox WHERE update_id=2').fetchone()[0],'intent_state':a.close_intent('p',1).state.value}

def overlapping_buys(t):
 with OfflineIntegration(t.idb,t.odb,owner='owner-fixture') as a:
  a.recover(t.exchange(a));a.receive(1,'owner-fixture','RESUME');a.process_close()
  sim=GuardedSubmit(t.edb);sim.integration=a
  outcomes=[]
  for k in ('buy-a','buy-b'):
   i=a.journal.prepare('probe',k,Request('BUY',quantity='0.001'))
   outcomes.append(a.dispatch(i.intent_id,sim).exchange_status)
  return {'statuses':outcomes,'submissions':t.sim.counts()['submissions']}

def full_partial(t):
 with OfflineIntegration(t.idb,t.odb,owner='owner-fixture') as a:
  i=setup(t,a);sim=GuardedSubmit(t.edb);sim.integration=a;a.dispatch(i.intent_id,sim)
  ex=t.exchange(a,i)
  f=Fill('exit-fill',a.journal.get(i.intent_id).exchange_order_id,i.client_order_id,2,'SELL','0.001','50','USDT','0')
  bad=RecoveryExchange('partial-full',[t.entry_fill,f],ex._orders,'1000','0')
  result=a.recover(bad)
  return {'result':result,'gate':tuple(a.journal.db.execute('SELECT blocked,reason FROM ledger_control').fetchone()),'reservation':tuple(a.journal.db.execute('SELECT active FROM close_reservations').fetchone()),'position':a.journal.db.execute('SELECT base_balance FROM ledger_account').fetchone()[0]}

def unaccounted_filled_entry(t):
 with OfflineIntegration(t.idb,t.odb,owner='owner-fixture') as a:
  a.recover(t.exchange(a));a.receive(1,'owner-fixture','RESUME');a.process_close()
  class Filled(GuardedSubmit):
   def submit(self,intent):
    r=super().submit(intent);return Receipt(r.exchange_order_id,'FILLED')
  sim=Filled(t.edb);sim.integration=a
  first=a.journal.prepare('probe','filled-entry',Request('BUY',quantity='0.001'))
  a.dispatch(first.intent_id,sim)
  second=a.journal.prepare('probe','next-entry',Request('BUY',quantity='0.001'))
  result=a.dispatch(second.intent_id,sim)
  return {'second_sent':result.state.value,'new_fills':a.journal.db.execute('SELECT COUNT(*) FROM ledger_fills WHERE intent_id=?',(first.intent_id,)).fetchone()[0],'gate':tuple(a.journal.db.execute('SELECT blocked,reason FROM ledger_control').fetchone())}

class ThreeFindings(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.IntegrationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
 def test_new_buy_requires_settlement(self):
  from trading_bot.order_journal import DispatchBlocked
  with self.assertRaises(DispatchBlocked):unaccounted_filled_entry(self.f)
 def test_rejected_close_allows_new_command(self):
  self.assertEqual(rejected_retry(self.f)['reply'],'CLOSE_INTENT_CREATED')
 def test_full_quantity_partial_status_blocks_recovery(self):
  result=full_partial(self.f);self.assertNotEqual(result['result'],'complete');self.assertEqual(result['gate'][0],1)

 def test_retry_rollback_and_restart_preserve_history(self):
  f=self.f
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   i=setup(f,a)
   class Reject:
    is_simulator=True
    def submit(self,i):return Receipt(None,'REJECTED')
   a.dispatch(i.intent_id,Reject());a.receive(2,'owner-fixture','CLOSE','p',1)
   def fault(p):
    if p=='command_before_commit':raise RuntimeError('simulated crash')
   a.fault=fault
   with self.assertRaises(RuntimeError):a.process_close()
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   self.assertEqual(a.journal.get(i.intent_id).state.value,'REJECTED')
   self.assertEqual(a.inbox.db.execute('SELECT status FROM updates WHERE update_id=2').fetchone()[0],'PENDING')
   a.process_close();new=a.close_intent('p',1)
   self.assertNotEqual(new.intent_id,i.intent_id)
   self.assertEqual(a.journal.get(i.intent_id).state.value,'REJECTED')
   a.receive(2,'owner-fixture','CLOSE','p',1);self.assertIsNone(a.process_close())
 def test_competing_new_entries_claim_only_one(self):
  import threading
  from trading_bot.order_journal import DispatchBlocked
  f=self.f;ready=threading.Barrier(3);go=threading.Event();out=[]
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as main:
   main.receive(1,'owner-fixture','RESUME');main.process_close()
   ids=[main.journal.prepare('race',str(n),Request('BUY',quantity='0.001')).intent_id for n in range(2)]
   def worker(i):
    with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
     ready.wait(timeout=10);go.wait(timeout=10)
     try:a._claim(i);out.append('claimed')
     except DispatchBlocked:out.append('blocked')
   threads=[threading.Thread(target=worker,args=(i,)) for i in ids]
   for t in threads:t.start()
   ready.wait(timeout=10);self.assertEqual(main.recover(f.exchange(main)),'complete');go.set()
   for t in threads:t.join(timeout=10)
   self.assertCountEqual(out,['claimed','blocked'])

 def test_retry_hard_crash_boundaries(self):
  import os,subprocess,sys
  for point in ('command_before_commit','command_committed'):
   f=fixtures.IntegrationTests();f.setUp()
   try:
    with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
     i=setup(f,a)
     class Reject:
      is_simulator=True
      def submit(self,i):return Receipt(None,'REJECTED')
     a.dispatch(i.intent_id,Reject());a.receive(2,'owner-fixture','CLOSE','p',1)
    script="""import os,sys
from trading_bot.offline_integration import OfflineIntegration
def fault(p):
 if p==sys.argv[3]:os._exit(87)
with OfflineIntegration(sys.argv[1],sys.argv[2],owner='owner-fixture',fault=fault) as a:a.process_close()
"""
    proc=subprocess.run([sys.executable,'-B','-c',script,str(f.idb),str(f.odb),point])
    self.assertEqual(proc.returncode,87)
    with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
     self.assertEqual(a.journal.get(i.intent_id).state.value,'REJECTED')
     status=a.inbox.db.execute('SELECT status FROM updates WHERE update_id=2').fetchone()[0]
     self.assertEqual(status,'PENDING' if point=='command_before_commit' else 'DONE')
     a.process_close();new=a.close_intent('p',1)
     self.assertNotEqual(new.intent_id,i.intent_id)
     self.assertEqual(a.journal.db.execute("SELECT COUNT(*) FROM order_intents WHERE namespace='synthetic-close'").fetchone()[0],2)
   finally:f.doCleanups()
