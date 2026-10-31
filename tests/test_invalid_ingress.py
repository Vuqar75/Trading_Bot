import unittest
import test_offline_integration as fixture
from trading_bot.offline_integration import OfflineIntegration

class InvalidIngressTests(unittest.TestCase):
 def setUp(self):
  self.f=fixture.IntegrationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
 def app(self):return OfflineIntegration(self.f.idb,self.f.odb,owner='owner-fixture')
 def test_unauthorized_and_invalid_then_pause(self):
  with self.app() as a:
   a.receive(1,'intruder','RETRY_CLOSE','p',1,'rejection')
   a.receive(2,'owner-fixture','RETRY_CLOSE','p',1)
   a.receive(3,'owner-fixture','PAUSE')
   self.assertEqual([tuple(r) for r in a.inbox.db.execute('SELECT status FROM updates ORDER BY update_id')],[('IGNORED',),('REJECTED',),('PENDING',)])
   self.assertEqual(a.process_close(),3)
  with self.app() as a:
   a.receive(1,'intruder','RETRY_CLOSE','p',1,'rejection')
   a.receive(2,'owner-fixture','RETRY_CLOSE','p',1)
   self.assertIsNone(a.process_close())
 def test_crash_after_rejection_commit_before_cleanup(self):
  with self.app() as a:
   def fault(point):
    if point=='receive_committed':raise RuntimeError('crash')
   a.inbox.fault=fault
   with self.assertRaises(RuntimeError):a.receive(1,'owner-fixture','RETRY_CLOSE','p',1)
  with self.app() as a:
   a.receive(1,'owner-fixture','RETRY_CLOSE','p',1)
   a.receive(2,'owner-fixture','PAUSE')
   self.assertEqual(a.process_close(),2)
   self.assertEqual(a.inbox.db.execute("SELECT COUNT(*) FROM updates WHERE status='REJECTED'").fetchone()[0],1)
 def test_real_process_crash_before_and_after_commit(self):
  import subprocess,sys
  script='''import os,sys
from trading_bot.offline_integration import OfflineIntegration
with OfflineIntegration(sys.argv[1],sys.argv[2],owner="owner-fixture") as a:
 def crash(p):
  if p==sys.argv[3]:os._exit(88)
 a.inbox.fault=crash
 a.receive(7,"owner-fixture","RETRY_CLOSE","p",1)
'''
  for point in ('receive_before_commit','receive_committed'):
   f=fixture.IntegrationTests();f.setUp()
   try:
    with OfflineIntegration(f.idb,f.odb,owner='owner-fixture'):pass
    p=subprocess.run([sys.executable,'-B','-c',script,str(f.idb),str(f.odb),point])
    self.assertEqual(p.returncode,88)
    with OfflineIntegration(f.idb,f.odb,owner='owner-fixture') as a:
     self.assertIsNotNone(a.receiver.spool.execute('SELECT * FROM receiver_pending').fetchone())
     count=a.inbox.db.execute("SELECT COUNT(*) FROM updates WHERE status='REJECTED'").fetchone()[0]
     self.assertEqual(count,int(point=='receive_committed'))
     a.receive(7,'owner-fixture','RETRY_CLOSE','p',1);a.receive(99,'owner-fixture','PAUSE')
     self.assertEqual(a.process_close(),99)
     self.assertEqual(a.inbox.db.execute("SELECT COUNT(*) FROM updates WHERE status='REJECTED'").fetchone()[0],1)
   finally:f.doCleanups()
 def test_internal_error_keeps_spool_and_is_not_rejection(self):
  with self.app() as a:
   def fault(point):
    if point=='receive_before_commit':raise RuntimeError('internal')
   a.inbox.fault=fault
   with self.assertRaisesRegex(RuntimeError,'internal'):a.receive(1,'owner-fixture','RETRY_CLOSE','p',1)
   self.assertIsNotNone(a.receiver.spool.execute('SELECT * FROM receiver_pending').fetchone())
   self.assertEqual(a.inbox.db.execute('SELECT COUNT(*) FROM updates').fetchone()[0],0)
