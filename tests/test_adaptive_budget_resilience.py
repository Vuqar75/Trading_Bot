from contextlib import closing
import sqlite3,tempfile,unittest
from pathlib import Path
from research.adaptive_budget_resilience import APP_ID,backup,check_backup,migrate
class BudgetBackupTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.p=Path(self.temp.name);(self.p/'interactive-once.lock').write_text('original');(self.p/'approved-followup-once.lock').write_text('second')
  db=sqlite3.connect(self.p/'experiment-budget.db');db.execute(f'pragma application_id={APP_ID}');db.execute('create table api_attempts(request_id text primary key,reserved_micro_usd integer,created_at real,result_json text)');db.executemany('insert into api_attempts values(?,?,?,?)',[('old',3630,1,None),('new',2088,2,None)]);db.commit();db.close()
 def tearDown(self):self.temp.cleanup()
 def test_backup_restore_and_migration_preserve_history_and_locks(self):
  folder,m=migrate(self.p);self.assertEqual(m['reserved_micro_usd'],5718);self.assertEqual(check_backup(folder),m)
  self.assertEqual((self.p/'interactive-once.lock').read_text(),'original');self.assertEqual((self.p/'approved-followup-once.lock').read_text(),'second')
  with closing(sqlite3.connect(self.p/'experiment-budget.db')) as db:self.assertEqual(db.execute('select count(*) from api_attempts').fetchone()[0],2);self.assertEqual(db.execute('select count(*) from adaptive_responses').fetchone()[0],0)
 def test_failed_migration_rolls_back(self):
  def fault(stage):
   if stage=='before_migration_commit':raise RuntimeError('crash')
  with self.assertRaises(RuntimeError):migrate(self.p,fault=fault)
  with closing(sqlite3.connect(self.p/'experiment-budget.db')) as db:self.assertIsNone(db.execute("select name from sqlite_master where name='adaptive_responses'").fetchone());self.assertEqual(db.execute('select sum(reserved_micro_usd) from api_attempts').fetchone()[0],5718)
 def test_corrupt_backup_is_rejected(self):
  folder,_=backup(self.p);(folder/'interactive-once.lock').write_text('damaged')
  with self.assertRaisesRegex(ValueError,'hash'):check_backup(folder)
