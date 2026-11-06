import io,sqlite3,tarfile,tempfile,threading,time,unittest
from contextlib import closing
from pathlib import Path
from research.adaptive_study_supervisor import bootstrap,cycle_lock
from research.adaptive_study_backup import create,verify,transfer,stage,upload_file,publish_status
from research.adaptive_study_supervisor import backup_health

class BackupTest(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)/'study';bootstrap(self.root,clock=lambda:1000)
 def test_external_readback_and_isolated_restore(self):
  manifest,blob=create(self.root,version='test',clock=lambda:2000)
  self.assertEqual(verify(blob)['start_ms'],1000)
  result=transfer(blob,manifest,transport=lambda name,data:data)
  self.assertIn('adaptive-continuous/',result['object'])
  with self.assertRaises(Exception):transfer(blob,manifest,transport=lambda name,data:b'corrupt')
  with self.assertRaises(TimeoutError):transfer(blob,manifest,transport=lambda name,data:(_ for _ in ()).throw(TimeoutError('offline')))
  self.assertTrue((self.root/'ai'/'financial.db').exists())
 def test_backup_waits_for_cycle_and_leaves_originals(self):
  results=[];errors=[]
  def worker():
   try:results.append(create(self.root,version='test',clock=lambda:2000))
   except Exception as exc:errors.append(exc)
  with cycle_lock(self.root):
   with closing(sqlite3.connect(self.root/'ai'/'observations.db')) as db:db.execute("INSERT INTO samples(stamp_ms,status,payload) VALUES(1,'test','{}')");db.commit()
   t=threading.Thread(target=worker);t.start();time.sleep(.1);self.assertTrue(t.is_alive())
  t.join(10);self.assertFalse(t.is_alive());self.assertFalse(errors);self.assertEqual(len(results),1)
  self.assertTrue((self.root/'ai'/'financial.db').exists());self.assertTrue((self.root/'baseline'/'financial.db').exists())
  with tarfile.open(fileobj=io.BytesIO(results[0][1]),mode='r:gz') as tar:raw=tar.extractfile('ai/observation.db').read()
  p=Path(self.tmp.name)/'restored-observation.db';p.write_bytes(raw)
  with closing(sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True)) as db:self.assertEqual(db.execute('SELECT count(*) FROM samples').fetchone()[0],1)
 def test_uploader_reads_completed_export_only(self):
  export=Path(self.tmp.name)/'export';export.mkdir(mode=0o750)
  path=stage(self.root,export,version='test',clock=lambda:2000)
  self.assertEqual(path.stat().st_mode&0o777,0o640)
  self.assertEqual(upload_file(path,transport=lambda name,blob:blob)['captured_ms'],2000)
  link=export/'link';link.symlink_to(path)
  with self.assertRaisesRegex(ValueError,'backup_file_invalid'):upload_file(link,transport=lambda name,blob:blob)
 def test_health_distinguishes_missing_fresh_and_stale_backup(self):
  import os
  status=Path(self.tmp.name)/'status';status.mkdir(mode=0o750)
  self.assertEqual(backup_health(status/'last-success.json',owner_uid=os.getuid(),now_ms=100)['state'],'unavailable')
  value={'object':'paper-backups/daily/adaptive-continuous/x','captured_ms':100,'verified_ms':101,'sha256':'a'*64}
  publish_status(status,value,group_gid=os.getgid())
  self.assertEqual(backup_health(status/'last-success.json',owner_uid=os.getuid(),now_ms=102)['state'],'fresh')
  self.assertEqual(backup_health(status/'last-success.json',owner_uid=os.getuid(),now_ms=100+37*3600000)['state'],'stale')
