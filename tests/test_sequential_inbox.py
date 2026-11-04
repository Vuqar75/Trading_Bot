import sqlite3,unittest
import test_offline_integration as fixtures
from trading_bot.offline_integration import OfflineIntegration
from trading_bot.order_journal import Receipt

class SequentialInboxTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.IntegrationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
 def test_close_after_reject_requires_confirmation(self):
  f=self.f
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   a.recover(f.exchange(a));a.inbox.set_simulated_position('p',1);a.receive(1,'owner-fixture','CLOSE','p',1);a.process_close();old=a.close_intent('p',1)
   class Reject:
    is_simulator=True
    def submit(self,i):return Receipt(None,'REJECTED')
   a.dispatch(old.intent_id,Reject());a.receive(10,'owner-fixture','CLOSE','p',1);a.process_close()
   self.assertEqual(a.close_intent('p',1).intent_id,old.intent_id)
   a.receive(20,'owner-fixture','RETRY_CLOSE','p',1,old.intent_id);a.process_close()
   self.assertNotEqual(a.close_intent('p',1).intent_id,old.intent_id)
   a.receive(20,'owner-fixture','RETRY_CLOSE','p',1,old.intent_id);self.assertIsNone(a.process_close())
 def test_lock_stops_later_updates_and_retry(self):
  from trading_bot.sequential_inbox import SequentialReceiver, ReceiveStopped
  f=self.f
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   receiver=SequentialReceiver(a,max_attempts=2,delay=0)
   lock=sqlite3.connect(f.odb,isolation_level=None);lock.execute('BEGIN EXCLUSIVE')
   a.inbox.db.execute('PRAGMA busy_timeout=0')
   try:
    with self.assertRaises(ReceiveStopped):receiver.receive_batch([(2,'owner-fixture','CLOSE','p',1),(8,'owner-fixture','PAUSE','',0)])
    self.assertEqual(a.inbox.db.execute('SELECT next_offset FROM inbox_state').fetchone()[0],0)
   finally:lock.rollback();lock.close()
   with self.assertRaises(ReceiveStopped):receiver.receive_batch([(8,'owner-fixture','PAUSE','',0)])
   receiver.close()
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   receiver=SequentialReceiver(a,max_attempts=2,delay=0);receiver.receive_batch([(2,'owner-fixture','CLOSE','p',1),(8,'owner-fixture','PAUSE','',0)])
   self.assertEqual(a.inbox.db.execute('SELECT next_offset FROM inbox_state').fetchone()[0],9)
   self.assertEqual(a.inbox.db.execute('SELECT COUNT(*) FROM updates').fetchone()[0],2)
   receiver.close()

 def test_restart_cannot_skip_pending(self):
  from trading_bot.sequential_inbox import SequentialReceiver,ReceiveStopped
  f=self.f
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   r=SequentialReceiver(a,max_attempts=1,delay=0);lock=sqlite3.connect(f.odb,isolation_level=None);lock.execute('BEGIN EXCLUSIVE');a.inbox.db.execute('PRAGMA busy_timeout=0')
   try:
    with self.assertRaises(ReceiveStopped):r.receive_batch([(7,'owner-fixture','CLOSE','p',1),(99,'owner-fixture','PAUSE','',0)])
   finally:lock.rollback();lock.close();r.close()
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   r=SequentialReceiver(a)
   with self.assertRaises(ReceiveStopped):r.receive_batch([(99,'owner-fixture','PAUSE','',0)])
   r.close();r=SequentialReceiver(a);r.receive_batch([(7,'owner-fixture','CLOSE','p',1),(99,'owner-fixture','PAUSE','',0)])
   self.assertEqual([x[0] for x in a.inbox.db.execute('SELECT update_id FROM updates ORDER BY update_id')],[7,99]);r.close()
 def test_accumulated_confirmation_cannot_confirm_future_rejection(self):
  f=self.f
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   a.recover(f.exchange(a));a.inbox.set_simulated_position('p',1);a.receive(1,'owner-fixture','CLOSE','p',1);a.process_close();i=a.close_intent('p',1)
   a.receive(2,'owner-fixture','RETRY_CLOSE','p',1,i.intent_id)
   class Reject:
    is_simulator=True
    def submit(self,i):return Receipt(None,'REJECTED')
   a.dispatch(i.intent_id,Reject());a.process_close()
   self.assertEqual(a.close_intent('p',1).intent_id,i.intent_id)
   a.receive(4,'owner-fixture','RETRY_CLOSE','p',1,i.intent_id);a.process_close();self.assertNotEqual(a.close_intent('p',1).intent_id,i.intent_id)
 def test_crash_after_commit_replays_without_duplicate(self):
  import subprocess,sys
  f=self.f
  script="""import os,sys
from trading_bot.offline_integration import OfflineIntegration
from trading_bot.sequential_inbox import SequentialReceiver
with OfflineIntegration(sys.argv[1],sys.argv[2],owner='owner-fixture') as a:
 def fault(p):
  if p=='receive_committed':os._exit(88)
 a.inbox.fault=fault
 SequentialReceiver(a).receive_batch([(4,'owner-fixture','CLOSE','p',1)])
"""
  proc=subprocess.run([sys.executable,'-B','-c',script,str(f.idb),str(f.odb)])
  self.assertEqual(proc.returncode,88)
  from trading_bot.sequential_inbox import SequentialReceiver
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   r=SequentialReceiver(a);r.receive_batch([(4,'owner-fixture','CLOSE','p',1),(40,'owner-fixture','PAUSE','',0)]);r.close()
   self.assertEqual(a.inbox.db.execute('SELECT COUNT(*) FROM updates').fetchone()[0],2)
   self.assertEqual(a.journal.db.execute('SELECT COUNT(*) FROM close_command_generation WHERE update_id=4').fetchone()[0],1)

 def test_pending_replay_accepts_equivalent_default_arguments(self):
  from trading_bot.sequential_inbox import ReceiveStopped
  f=self.f
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   lock=sqlite3.connect(f.odb,isolation_level=None);lock.execute('BEGIN EXCLUSIVE');a.inbox.db.execute('PRAGMA busy_timeout=0')
   try:
    with self.assertRaises(ReceiveStopped):a.receiver.receive_batch([(7,'owner-fixture','CLOSE','p',1)])
   finally:lock.rollback();lock.close()
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   a.receive(7,'owner-fixture','CLOSE','p',1)
   self.assertEqual(a.inbox.db.execute('SELECT COUNT(*) FROM updates WHERE update_id=7').fetchone()[0],1)

 def test_spool_does_not_store_sender_and_conflicting_confirmation_is_rejected(self):
  from trading_bot.telegram_inbox import Conflict
  f=self.f
  with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
   a.recover(f.exchange(a));a.inbox.set_simulated_position('p',1);a.receive(1,'owner-fixture','CLOSE','p',1);a.process_close();i=a.close_intent('p',1)
   class Reject:
    is_simulator=True
    def submit(self,i):return Receipt(None,'REJECTED')
   a.dispatch(i.intent_id,Reject());a.receive(2,'owner-fixture','RETRY_CLOSE','p',1,i.intent_id)
   with self.assertRaises(Conflict):a.receive(2,'owner-fixture','RETRY_CLOSE','p',1,'different-rejection')
   self.assertEqual(a.journal.db.execute('SELECT rejected_intent FROM retry_confirmations WHERE update_id=2').fetchone()[0],i.intent_id)
   from trading_bot.sequential_inbox import SequentialReceiver,ReceiveStopped
   r=SequentialReceiver(a,max_attempts=1,delay=0);lock=sqlite3.connect(f.odb,isolation_level=None);lock.execute('BEGIN EXCLUSIVE');a.inbox.db.execute('PRAGMA busy_timeout=0')
   try:
    with self.assertRaises(ReceiveStopped):r.receive_batch([(3,'owner-fixture','CLOSE','p',1)])
    payload=r.spool.execute('SELECT payload FROM receiver_pending').fetchone()[0]
    self.assertNotIn('owner-fixture',payload)
   finally:lock.rollback();lock.close();r.close()
