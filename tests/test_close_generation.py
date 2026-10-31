import unittest
import test_offline_integration as fixtures
from trading_bot.offline_integration import OfflineIntegration
from trading_bot.order_journal import Receipt

def setup(t,a):
 assert a.recover(t.exchange(a))=='complete'
 a.inbox.set_simulated_position('p',1)
 a.receive(1,'owner-fixture','CLOSE','p',1);a.process_close()
 return a.close_intent('p',1)


class Reject:
 is_simulator=True
 def submit(self,i):return Receipt(None,'REJECTED')
def queued_before_reject(t):
 with OfflineIntegration(t.idb,t.odb,owner='owner-fixture') as a:
  i=setup(t,a)
  a.receive(2,'owner-fixture','CLOSE','p',1)
  a.dispatch(i.intent_id,Reject())
  a.process_close();new=a.close_intent('p',1)
  return {'new_intent':new.intent_id!=i.intent_id,'new_state':new.state.value,'result':a.inbox.db.execute('SELECT result FROM outbox WHERE update_id=2').fetchone()[0]}
def long_retry(t):
 with OfflineIntegration(t.idb,t.odb,owner='owner-fixture') as a:
  a.recover(t.exchange(a));target='p'*64;version=9223372036854775807
  a.inbox.set_simulated_position(target,version);a.receive(1,'owner-fixture','CLOSE',target,version);a.process_close();i=a.close_intent(target,version)
  a.dispatch(i.intent_id,Reject());uid=9223372036854775806
  a.receive(uid,'owner-fixture','CLOSE',target,version)
  errors=[]
  for _ in range(2):
   try:a.process_close()
   except ValueError as e:errors.append(str(e))
  return {'errors':errors,'status':a.inbox.db.execute('SELECT status FROM updates WHERE update_id=?',(uid,)).fetchone()[0]}

class CloseGenerationTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.IntegrationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
 def test_queued_before_rejected_does_not_retry(self):
  self.assertFalse(queued_before_reject(self.f)['new_intent'])
 def test_large_identifiers_do_not_stall(self):
  result=long_retry(self.f);self.assertEqual(result['errors'],[]);self.assertEqual(result['status'],'DONE')

 def test_multiple_queued_commands_then_explicit_retry_after_restart(self):
  f=self.f
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   i=setup(f,a)
   for n in (2,3,4):a.receive(n,'owner-fixture','CLOSE','p',1)
   a.dispatch(i.intent_id,Reject())
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   a.receive(5,'owner-fixture','CLOSE','p',1)
   for _ in range(4):a.process_close()
   self.assertEqual([r[0] for r in a.inbox.db.execute('SELECT result FROM outbox WHERE update_id>1 ORDER BY update_id')],['CLOSE_ALREADY_PENDING']*3+['CLOSE_INTENT_CREATED'])
   self.assertNotEqual(a.close_intent('p',1).intent_id,i.intent_id)
 def test_long_retry_allows_next_update(self):
  f=self.f
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   a.recover(f.exchange(a));target='x'*64;version=9223372036854775807
   a.inbox.set_simulated_position(target,version);a.receive(1,'owner-fixture','CLOSE',target,version);a.process_close();i=a.close_intent(target,version)
   a.dispatch(i.intent_id,Reject());a.receive(10000000000,'owner-fixture','CLOSE',target,version);a.receive(10000000001,'owner-fixture','PAUSE')
   a.process_close();a.process_close()
   self.assertEqual(a.inbox.db.execute('SELECT status FROM updates WHERE update_id=10000000001').fetchone()[0],'DONE')
 def test_concurrent_receive_and_rejection_linearize(self):
  import threading, sqlite3, time
  f=self.f;barrier=threading.Barrier(3);out=[]
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as main:
   i=setup(f,main);main._claim(i.intent_id)
   def receive():
    with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
     barrier.wait(timeout=10)
     for attempt in range(100):
      try:a.receive(2,'owner-fixture','CLOSE','p',1);out.append('received');break
      except sqlite3.OperationalError as e:
       if 'locked' not in str(e):raise
       time.sleep(0.01)
   def reject():
    with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
     barrier.wait(timeout=10);a.journal.record_receipt(i.intent_id,Receipt(None,'REJECTED'));out.append('rejected')
   ts=[threading.Thread(target=receive),threading.Thread(target=reject)]
   for t in ts:t.start()
   barrier.wait(timeout=10)
   for t in ts:t.join(timeout=10)
   self.assertCountEqual(out,['received','rejected'])
   captured=main.journal.db.execute('SELECT observed_state FROM close_command_generation WHERE update_id=2').fetchone()[0]
   main.process_close()
   result=main.inbox.db.execute('SELECT result FROM outbox WHERE update_id=2').fetchone()[0]
   self.assertEqual(result,'CLOSE_INTENT_CREATED' if captured=='REJECTED' else 'CLOSE_ALREADY_PENDING')

 def test_receive_rollback_preserves_generation_and_offset(self):
  f=self.f
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   i=setup(f,a);a.dispatch(i.intent_id,Reject())
   def fault(point):
    if point=='receive_before_commit':raise RuntimeError('crash')
   a.inbox.fault=fault
   with self.assertRaises(RuntimeError):a.receive(2,'owner-fixture','CLOSE','p',1)
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   self.assertIsNone(a.inbox.db.execute('SELECT 1 FROM updates WHERE update_id=2').fetchone())
   self.assertIsNone(a.journal.db.execute('SELECT 1 FROM close_command_generation WHERE update_id=2').fetchone())
   a.receive(2,'owner-fixture','CLOSE','p',1);a.process_close()
   self.assertEqual(a.inbox.db.execute('SELECT result FROM outbox WHERE update_id=2').fetchone()[0],'CLOSE_INTENT_CREATED')
 def test_legacy_retry_remains_current_and_blocks_duplicate(self):
  f=self.f
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   i=setup(f,a);a.dispatch(i.intent_id,Reject());a.receive(2,'owner-fixture','CLOSE','p',1);a.process_close();retry=a.close_intent('p',1)
   a.journal.db.execute('UPDATE order_intents SET business_key=? WHERE intent_id=?',('close-p-1-r2',retry.intent_id))
   self.assertEqual(a.close_intent('p',1).intent_id,retry.intent_id)
   a.receive(3,'owner-fixture','CLOSE','p',1);a.process_close()
   self.assertEqual(a.inbox.db.execute('SELECT result FROM outbox WHERE update_id=3').fetchone()[0],'CLOSE_ALREADY_PENDING')
   self.assertEqual(a.journal.db.execute("SELECT COUNT(*) FROM order_intents WHERE namespace='synthetic-close'").fetchone()[0],2)
