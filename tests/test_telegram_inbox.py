import os, sqlite3, subprocess, sys, tempfile, unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from trading_bot.telegram_inbox import Inbox, Update, Conflict, DeliveryUncertain, ResponseSimulator

WORKER='''import os,sys\nfrom trading_bot.telegram_inbox import Inbox,Update,ResponseSimulator\npath,sp,stage,point=sys.argv[1:]\ndef crash(p):\n if p==point:os._exit(83)\nwith Inbox(path,fault=crash) as box:\n if stage=="receive":box.receive_batch([Update(10,True,"PAUSE")])\n elif stage=="process":box.process_one()\n else:box.send_one(ResponseSimulator(sp))\n'''

class InboxTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name); self.db=self.root/'inbox.db'; self.simpath=self.root/'sim.db'
 def calls(self):
  with closing(sqlite3.connect(self.simpath)) as d:return d.execute('SELECT count(*) FROM response_calls').fetchone()[0]
 def test_receive_offsets_duplicates_and_authorization(self):
  with Inbox(self.db) as b:
   b.receive_batch([Update(1,False),Update(2,True,'PAUSE')]); b.receive_batch([Update(2,True,'PAUSE')])
   self.assertEqual(b.db.execute('SELECT next_offset FROM inbox_state').fetchone()[0],3)
   self.assertEqual(b.process_one(),2); self.assertIsNone(b.process_one())
   with self.assertRaises(Conflict):b.receive_batch([Update(2,True,'RESUME')])
   with self.assertRaises(Conflict):b.receive_batch([Update(0,True,'RESUME')])
   self.assertEqual(b.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)
 def test_pause_resume_order_and_duplicate_delivery(self):
  with Inbox(self.db) as b:
   b.receive_batch([Update(2,True,'RESUME'),Update(5,True,'PAUSE')]); b.process_one(); b.process_one()
   b.receive_batch([Update(2,True,'RESUME')]); self.assertIsNone(b.process_one())
   self.assertEqual(b.db.execute('SELECT paused FROM inbox_state').fetchone()[0],1)
 def test_close_targets_and_distinct_updates(self):
  with Inbox(self.db) as b:
   b.set_simulated_position('p',1)
   b.receive_batch([Update(1,True,'CLOSE','p',1),Update(2,True,'CLOSE','p',1)])
   b.process_one(); b.process_one()
   self.assertEqual(b.db.execute('SELECT count(*) FROM closing_intents').fetchone()[0],1)
   self.assertEqual([r[0] for r in b.db.execute('SELECT result FROM outbox ORDER BY update_id')],['CLOSE_INTENT_CREATED','CLOSE_ALREADY_PENDING'])
   b.receive_batch([Update(3,True,'CLOSE','p',1)]); b.set_simulated_position('p',2); b.process_one()
   self.assertEqual(b.db.execute('SELECT result FROM outbox WHERE update_id=3').fetchone()[0],'STALE_POSITION')
 def test_conflict_rolls_back_complete_batch(self):
  with Inbox(self.db) as b:
   b.receive_batch([Update(2,True,'PAUSE')])
   with self.assertRaises(Conflict):b.receive_batch([Update(2,True,'STOP'),Update(4,True,'RESUME')])
   self.assertEqual(b.db.execute('SELECT next_offset FROM inbox_state').fetchone()[0],3)
   self.assertEqual(b.db.execute('SELECT count(*) FROM updates').fetchone()[0],1)
 def test_timeout_after_delivery_never_reexecutes_command(self):
  sim=ResponseSimulator(self.simpath,'timeout_after')
  with Inbox(self.db) as b:
   b.set_simulated_position('p',1); b.receive_batch([Update(1,True,'CLOSE','p',1)]); b.process_one()
   with self.assertRaises(DeliveryUncertain): b.send_one(sim)
   self.assertEqual(b.db.execute('SELECT status FROM outbox').fetchone()[0],'SENDING')
  with Inbox(self.db) as b:
   self.assertIsNone(b.process_one()); b.requeue_uncertain(); b.send_one(ResponseSimulator(self.simpath))
   self.assertEqual(b.db.execute('SELECT count(*) FROM closing_intents').fetchone()[0],1)
   self.assertEqual(b.db.execute('SELECT status FROM outbox').fetchone()[0],'SENT')
  self.assertEqual(self.calls(),2)
 def test_competing_workers_execute_once(self):
  with Inbox(self.db) as b:b.set_simulated_position('p',1); b.receive_batch([Update(1,True,'CLOSE','p',1)])
  def work(_):
   with Inbox(self.db) as b:return b.process_one()
  with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(work,range(2)))
  self.assertEqual(sum(r is not None for r in results),1)
  with Inbox(self.db) as b:self.assertEqual(b.db.execute('SELECT count(*) FROM closing_intents').fetchone()[0],1)
 def test_foreign_database_rejected(self):
  with closing(sqlite3.connect(self.db)) as d:d.execute('CREATE TABLE bot_state(x)');d.commit()
  with self.assertRaises(ValueError):Inbox(self.db)
 def test_no_network_needed(self):
  with patch('socket.socket',side_effect=AssertionError('network')),patch('socket.create_connection',side_effect=AssertionError('network')):
   sim=ResponseSimulator(self.simpath)
   with Inbox(self.db) as b:b.receive_batch([Update(1,True,'PAUSE')]); b.process_one(); b.send_one(sim)
 def test_hard_crash_receive_boundaries(self):
  for point in ('receive_written','receive_before_commit','receive_committed'):
   db=self.root/(point+'.db')
   p=subprocess.run([sys.executable,'-B','-c',WORKER,str(db),str(self.simpath),'receive',point]); self.assertEqual(p.returncode,83)
   with Inbox(db) as b:
    count=b.db.execute('SELECT COUNT(*) FROM updates').fetchone()[0]
    self.assertEqual(count,1 if point=='receive_committed' else 0)
    b.receive_batch([Update(10,True,'PAUSE')]); b.process_one()
    self.assertEqual(b.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)
 def test_hard_crash_process_boundaries(self):
  for point in ('effect_written','process_before_commit','process_committed'):
   db=self.root/(point+'.db')
   with Inbox(db) as b:b.set_simulated_position('p',1);b.receive_batch([Update(1,True,'CLOSE','p',1)])
   p=subprocess.run([sys.executable,'-B','-c',WORKER,str(db),str(self.simpath),'process',point]);self.assertEqual(p.returncode,83)
   with Inbox(db) as b:
    b.process_one();b.process_one()
    self.assertEqual(b.db.execute('SELECT COUNT(*) FROM closing_intents').fetchone()[0],1)
    self.assertEqual(b.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)
 def test_hard_crash_send_boundaries(self):
  for point in ('send_claim_before_commit','send_claim_committed','send_returned','send_result_before_commit','send_result_committed'):
   db=self.root/(point+'.db'); sp=self.root/(point+'-sim.db')
   with Inbox(db) as b:b.receive_batch([Update(1,True,'PAUSE')]);b.process_one()
   p=subprocess.run([sys.executable,'-B','-c',WORKER,str(db),str(sp),'send',point]);self.assertEqual(p.returncode,83)
   with Inbox(db) as b:
    self.assertIsNone(b.process_one()); b.requeue_uncertain();b.send_one(ResponseSimulator(sp))
    self.assertEqual(b.db.execute('SELECT count(*) FROM updates WHERE status=\'DONE\'').fetchone()[0],1)
    self.assertEqual(b.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
    self.assertEqual(b.db.execute('SELECT status FROM outbox').fetchone()[0],'SENT')
if __name__=='__main__':unittest.main()
