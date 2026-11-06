import json,sqlite3,tempfile,unittest
from contextlib import closing
from pathlib import Path
from research.adaptive_budget_resilience import APP_ID,backup
from research.adaptive_regular_backup import pack,verify_plain,transfer
class RegularBackupTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.p=Path(self.t.name)
  for name in ('interactive-once.lock','approved-followup-once.lock','adaptive-next-once.lock'):(self.p/name).write_text('lock')
  (self.p/'server-owner.json').write_text(json.dumps({'role':'ACTIVE_SERVER','migration_id':'test'}))
  with closing(sqlite3.connect(self.p/'experiment-budget.db')) as db:
   db.execute(f'pragma application_id={APP_ID}');db.execute('create table api_attempts(request_id text primary key,reserved_micro_usd integer,created_at real,result_json text)');db.execute("insert into api_attempts values('x',7806,1,NULL)");db.commit()
 def test_archive_restores_and_failed_transfer_is_rejected(self):
  folder,m=backup(self.p);raw=pack(folder,self.p);self.assertEqual(verify_plain(raw),m)
  self.assertTrue(transfer(b'encrypted',m,lambda n,b:b)[0].startswith('paper-backups/daily/adaptive-ai-budget/regular/'))
  with self.assertRaisesRegex(ValueError,'readback'):transfer(b'encrypted',m,lambda n,b:b'corrupt')
 def test_uncommitted_concurrent_write_not_in_archive(self):
  db=sqlite3.connect(self.p/'experiment-budget.db');db.execute("insert into api_attempts values('pending',100,2,NULL)")
  try:folder,m=backup(self.p);self.assertEqual(verify_plain(pack(folder,self.p))['attempts'],1)
  finally:db.rollback();db.close()
 def test_corrupt_archive_rejected(self):
  folder,_=backup(self.p);raw=pack(folder,self.p)
  with self.assertRaises(Exception):verify_plain(raw[:20])
