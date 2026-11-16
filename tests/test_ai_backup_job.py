import json,os,sqlite3,tempfile,time,threading,unittest
from pathlib import Path
from unittest.mock import patch
from research.ai_limit_backup_job import BackupJob
from research.ai_limit_book import Book
from tests.test_ai_quote_stream import QuoteStreamTests

class BackupTests(unittest.TestCase):
 def test_old_inline_delay_reproduced_but_submit_does_not_wait_or_overlap(self):
  with tempfile.TemporaryDirectory() as root:
   entered=threading.Event();release=threading.Event();calls=[]
   def slow(stamp):calls.append(stamp);entered.set();release.wait(1);return {'fixture':True}
   # Same intentionally slow operation, executed inline as the former loop did.
   started=time.monotonic();time.sleep(.03);old=time.monotonic()-started
   job=BackupJob(root,action=slow);started=time.monotonic();self.assertTrue(job.submit(1));self.assertLess(time.monotonic()-started,old)
   self.assertTrue(entered.wait(1));self.assertFalse(job.submit(2));release.set();job.thread.join(1);self.assertEqual(calls,[1]);self.assertEqual(job.status['state'],'complete')
 def test_serving_loop_has_no_inline_backup_or_wait(self):
  import ast,inspect
  from research.ai_limit_runtime import main
  tree=ast.parse(inspect.getsource(main));serve=next(n for n in ast.walk(tree) if isinstance(n,ast.If) and ast.unparse(n.test)=="args.action == 'serve'")
  calls=[n.func.attr for stmt in serve.body for n in ast.walk(stmt) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)]
  self.assertNotIn('backup',calls);self.assertIn('submit',calls)
 def test_real_readonly_copy_is_consistent_and_source_history_unchanged(self):
  with tempfile.TemporaryDirectory() as root:
   directory=Path(root)/'paper';b=Book(directory);before=b.snapshot();job=BackupJob(directory)
   try:
    self.assertTrue(job.submit(100));job.thread.join(3);self.assertEqual(job.status['state'],'complete');self.assertTrue(job.status['source_readonly']);self.assertTrue(job.status['restored_balanced'])
    backup=directory/'backups/100.sqlite';self.assertEqual(backup.stat().st_mode&0o777,0o600)
    db=sqlite3.connect(backup);self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok');self.assertEqual(db.execute('SELECT count(*) FROM ledger_events').fetchone()[0],1);db.close();self.assertEqual(before['balances'],b.snapshot()['balances'])
   finally:b.close();job.close()
 def test_deadline_failure_is_terminal_and_removes_only_new_pending_file(self):
  with tempfile.TemporaryDirectory() as root:
   b=Book(Path(root)/'paper');job=BackupJob(b.root,timeout=-1)
   try:
    job.submit(1);job.thread.join(3);self.assertEqual(job.status['state'],'failed');self.assertFalse((b.root/'backups/1.pending').exists());self.assertFalse((b.root/'backups/1.sqlite').exists());self.assertTrue(b.verify()['balanced'])
   finally:b.close();job.close()
 def test_existing_archive_is_not_overwritten(self):
  with tempfile.TemporaryDirectory() as root:
   b=Book(Path(root)/'paper');folder=b.root/'backups';folder.mkdir();target=folder/'1.sqlite';target.write_bytes(b'unique')
   job=BackupJob(b.root)
   try:job.submit(1);job.thread.join(3);self.assertEqual(job.status['state'],'failed');self.assertEqual(target.read_bytes(),b'unique');self.assertTrue(b.verify()['balanced'])
   finally:b.close();job.close()

class BackupProtectionTests(QuoteStreamTests):
 def run_during_slow_backup(self,action):
  entered=threading.Event();release=threading.Event()
  def slow(stamp):entered.set();release.wait();return {}
  job=BackupJob(self.root,action=slow)
  try:
   job.submit(1);self.assertTrue(entered.wait(1));start=time.monotonic();action();print('protected_action_wall_s',round(time.monotonic()-start,6));self.assertFalse(release.is_set());self.assertTrue(job.thread.is_alive());self.assertTrue(self.b.verify()['balanced'])
  finally:release.set();job.thread.join(1)
 def test_actual_partial_position_backup_restores_ledger_and_state(self):
  import shutil
  self.opened();before=self.b.plans();balances=self.b.snapshot()['balances'];job=BackupJob(self.root)
  try:
   job.submit(100);job.thread.join(3);self.assertEqual(job.status['state'],'complete')
   restored=self.root.parent/'restored';restored.mkdir(mode=0o700);shutil.copyfile(self.root/'backups/100.sqlite',restored/'paper.sqlite')
   clone=Book(restored,clock=lambda:self.now)
   try:self.assertEqual(clone.plans(),before);self.assertEqual(clone.snapshot()['balances'],balances);self.assertTrue(clone.verify()['balanced'])
   finally:clone.close()
  finally:job.close()
 def test_open_partial_stop_is_processed_while_backup_is_waiting(self):
  self.opened()
  def protect():
   self.now+=1;self.book(100,'104','.1');self.mark('104');self.poller.cycle();self.assertEqual(self.b.active()[1]['open_qty'],'0.1')
   self.now+=1;self.book(101,'105','.1');self.poller.cycle();self.assertEqual(self.b.plans()[0][1]['open_qty'],'0');self.assertEqual(self.b.snapshot()['fills'],3)
  self.run_during_slow_backup(protect)
 def test_time_detected_without_market_while_backup_waits_then_fresh_exit(self):
  p=self.opened();opened=self.b.active()[1]['opened_ms']
  def protect():
   self.now=opened+p['hold_ms'];self.poller.cycle();self.assertEqual(self.b.active()[1]['close_reason'],'TIME');self.assertEqual(self.b.active()[1]['status'],'EXIT_PENDING')
   trigger=self.b.active()[1]['protection_trigger'];self.book(100,'100');self.mark('100');self.poller.cycle();self.assertEqual(self.b.plans()[0][1]['open_qty'],'0');self.assertEqual(self.b.plans()[0][1]['protection_trigger'],trigger)
  self.run_during_slow_backup(protect)

if __name__=='__main__':unittest.main()
