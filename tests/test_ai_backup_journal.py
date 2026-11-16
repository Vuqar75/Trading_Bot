import sqlite3,tempfile,time,threading,json,hashlib,unittest
from pathlib import Path
from contextlib import closing
from research.ai_limit_book import Book
from research.ai_limit_backup_job import BackupJob,prepare_journal
from tests.test_ai_phase_probe import InstrumentedProtectionTests
from tests.test_ai_backup_job import BackupProtectionTests
from tests.test_ai_quote_stream import QuoteStreamTests

class JournalTests(unittest.TestCase):
 def test_continuous_metadata_writes_restart_delete_copy_but_wal_read_snapshot_completes(self):
  results={}
  for mode in ['delete','wal']:
   with tempfile.TemporaryDirectory() as root:
    b=Book(Path(root)/'paper');payload=json.dumps({'fixture':'x'*20000});fp=hashlib.sha256(payload.encode()).hexdigest()
    with b.transaction():b.db.executemany('INSERT INTO bracket_inputs VALUES(?,?,?)',[(str(i),fp,payload) for i in range(400)])
    if mode=='wal':self.assertEqual(prepare_journal(b),'wal')
    stop=threading.Event();writes=[]
    def churn():
     db=sqlite3.connect(b.root/'paper.sqlite',isolation_level=None,timeout=.05)
     try:
      while not stop.is_set():
       try:db.execute("INSERT OR REPLACE INTO bracket_meta VALUES('fixture_churn',?)",(json.dumps(len(writes)),));writes.append(1)
       except sqlite3.OperationalError:pass
       stop.wait(.05)
     finally:db.close()
    thread=threading.Thread(target=churn);thread.start();previous=[None];restarts=[0];deadline=time.monotonic()+(1.5 if mode=='delete' else 8);done=False
    def progress(status,remaining,total):
     restarts[0]+=int(previous[0] is not None and remaining>previous[0]);previous[0]=remaining
     if time.monotonic()>deadline:raise TimeoutError()
     time.sleep(.005) # fixture-only slow backup step; no live process delay
    try:
     with closing(sqlite3.connect((b.root/'paper.sqlite').resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(Path(root)/'copy.sqlite')) as dst:
      if mode=='wal':src.execute('BEGIN');src.execute('SELECT count(*) FROM ledger_events').fetchone()
      try:src.backup(dst,pages=32,progress=progress,sleep=.001);done=True
      except TimeoutError:pass
     results[mode]={'completed':done,'restarts':restarts[0],'concurrent_writes':len(writes)}
     self.assertTrue(b.verify()['balanced'])
    finally:stop.set();thread.join(2);b.close()
  print('journal_reproduction',results)
  self.assertFalse(results['delete']['completed']);self.assertGreater(results['delete']['restarts'],0);self.assertTrue(results['wal']['completed']);self.assertEqual(results['wal']['restarts'],0);self.assertGreater(results['wal']['concurrent_writes'],0)
 def test_wal_backup_matches_existing_immutable_external_restore_inspector(self):
  import ast
  from decimal import Decimal
  tree=ast.parse((Path(__file__).resolve().parent.parent/'deploy/ai_bracket_external_backup.py').read_text())
  function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='inspect')
  scope={'Path':Path,'sqlite3':sqlite3,'Decimal':Decimal}
  exec(compile(ast.Module(body=[function],type_ignores=[]),'existing-inspector','exec'),scope)
  inspect=scope['inspect']
  with tempfile.TemporaryDirectory() as root:
   b=Book(Path(root)/'paper');prepare_journal(b);job=BackupJob(b.root)
   try:
    job.submit(1);job.thread.join(4);self.assertEqual(job.status['state'],'complete')
    summary=inspect(b.root/'backups/1.sqlite');self.assertEqual(summary['events'],1);self.assertEqual(summary['fills'],0)
   finally:job.close();b.close()
 def test_actual_wal_backup_read_snapshot_reopens_and_preserves_full_sync(self):
  with tempfile.TemporaryDirectory() as root:
   b=Book(Path(root)/'paper');self.assertEqual(prepare_journal(b),'wal');self.assertEqual(prepare_journal(b),'wal');self.assertEqual(b.db.execute('PRAGMA synchronous').fetchone()[0],2);job=BackupJob(b.root)
   try:
    job.submit(1);job.thread.join(4);self.assertEqual(job.status['state'],'complete');self.assertEqual(job.status['progress']['source_journal'],'wal');self.assertEqual(job.status['progress']['restarts'],0);self.assertTrue(job.status['restored_balanced']);self.assertTrue(b.verify()['balanced'])
   finally:job.close();b.close()

class WALProtectionTests(InstrumentedProtectionTests):
 def setUp(self):super().setUp();self.assertEqual(prepare_journal(self.b),'wal')
class WALBackupProtectionTests(BackupProtectionTests):
 def setUp(self):super().setUp();self.assertEqual(prepare_journal(self.b),'wal')

if __name__=='__main__':unittest.main()

class LegacyJournalGuardTests(QuoteStreamTests):
 def test_existing_partial_risk_keeps_old_mode_and_continues_protection(self):
  self.opened();self.assertEqual(prepare_journal(self.b),'delete')
  self.now+=1;self.book(100,'104');self.mark('104');self.poller.cycle()
  self.assertEqual(self.b.plans()[0][1]['open_qty'],'0');self.assertTrue(self.b.verify()['balanced'])
