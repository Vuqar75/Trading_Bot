import sqlite3,tempfile,unittest
from pathlib import Path
from research.adaptive_external_backup import snapshot,package,verify,transfer
class BackupTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.p=Path(self.temp.name);self.db=sqlite3.connect(self.p/'source.db');self.db.execute('create table snapshots(id text primary key,as_of_ms integer,payload text,baseline text)');self.db.execute('create table decisions(boundary_ms integer primary key,snapshot_id text,baseline text,ai_status text)');self.db.execute("insert into snapshots values('a',100,'{}','RANGE')");self.db.execute("insert into decisions values(100,'a','RANGE','unavailable')");self.db.commit()
 def tearDown(self):self.db.close();self.temp.cleanup()
 def test_snapshot_restore_and_readback(self):
  m,raw=snapshot(self.p/'source.db',self.p/'copy.db',clock=lambda:1000);blob=package(m,raw);self.assertEqual(verify(blob),m)
  out=transfer(blob,m,transport=lambda name,data:data);self.assertTrue(out['object'].startswith('paper-backups/daily/adaptive-shadow/'))
 def test_corrupt_or_failed_upload_is_rejected(self):
  m,raw=snapshot(self.p/'source.db',self.p/'copy.db');blob=package(m,raw)
  with self.assertRaises(Exception):verify(blob[:20])
  with self.assertRaises(ValueError):transfer(blob,m,transport=lambda name,data:b'bad')
 def test_uncommitted_concurrent_write_does_not_enter_copy(self):
  self.db.execute("insert into snapshots values('b',200,'{}','RANGE')")
  m,raw=snapshot(self.p/'source.db',self.p/'copy.db');self.assertEqual(m['counts']['snapshots'],1);self.assertEqual(m['latest_snapshot_ms'],100)
