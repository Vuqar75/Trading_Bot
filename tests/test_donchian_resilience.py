import fcntl,hashlib,json,os,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from research import donchian_prospective as p,donchian_service as s,donchian_external_backup as b

class ResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/'observer.sqlite';p.init(self.path,s.ORIGINAL,1000);self.db=p.connect(self.path)
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def test_upgrade_is_atomic_idempotent_and_keeps_start(self):
        s.upgrade(self.db,'abc',clock=lambda:2000);s.upgrade(self.db,'abc',clock=lambda:3000)
        self.assertEqual(p.meta(self.db,'start_ms'),1000);self.assertEqual(self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
        data=json.loads(self.db.execute('SELECT payload FROM events').fetchone()[0]);self.assertEqual(data['effective_ms'],2000)
        with self.assertRaisesRegex(ValueError,'adapter_version_changed'):s.upgrade(self.db,'other')
    def test_signal_rollback_and_committed_upgrade_survive_restart(self):
        self.db.execute('BEGIN IMMEDIATE');p.event(self.db,'uncommitted',1,{});self.db.close();self.db=p.connect(self.path)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)
        s.upgrade(self.db,'abc',clock=lambda:2000);self.db.close();self.db=p.connect(self.path);s.upgrade(self.db,'abc')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
    def test_competing_process_fails_before_upgrading(self):
        with open(str(self.path)+'.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=subprocess.run([sys.executable,'-B','-m','research.donchian_service','--state',str(self.path)],capture_output=True,text=True,timeout=15,env=dict(os.environ,PYTHONPATH='.'))
            self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)
    def test_recent_trade_must_be_fresh_and_not_future(self):
        def run(ts):
            clock=iter([100000,100100]);requests=iter([{'serverTime':100010},[{'id':42,'time':ts,'price':'100'}],{'serverTime':100090}])
            return s.timed_quote(self.db,'BTCUSDT','abc',request=lambda *x:next(requests),clock=lambda:next(clock),sleep=lambda _:None)
        self.assertEqual(run(100050)['source_trade_ms'],100050)
        for invalid in (90000,100101):
            with self.assertRaisesRegex(ValueError,'stale_or_future'):run(invalid)
    def test_trade_must_follow_committed_signal(self):
        self.db.execute('INSERT INTO quotes VALUES(?,?,?,NULL)',('100000:BTCUSDT','claimed',100000))
        for sc in ('base','stress'):self.db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?)',(100000,'BTCUSDT',sc,'BUY','90',100000,100050,'x','x','v'))
        clock=iter([100060,100100,100110,100150,100160,100200]);responses=iter(sum(([{'serverTime':100070},[{'id':1,'time':100049,'price':'100'}],{'serverTime':100090}] for _ in range(3)),[]))
        with self.assertRaisesRegex(ValueError,'no_trade_after_signal'):s.timed_quote(self.db,'BTCUSDT','abc',request=lambda *x:next(responses),clock=lambda:next(clock),sleep=lambda _:None)
    def test_snapshot_readback_and_corruption(self):
        s.upgrade(self.db,'abc',clock=lambda:2000);manifest,raw=b.snapshot(self.path,self.root/'copy.sqlite',clock=lambda:3);blob=b.package(manifest,raw)
        out=b.transfer(blob,manifest,transport=lambda name,data:data);self.assertTrue(out['object'].startswith(b.PREFIX));self.assertEqual(b.verify(blob)['start_ms'],1000)
        with self.assertRaises(Exception):b.transfer(blob,manifest,transport=lambda name,data:data[:-5])
        with self.assertRaises(OSError):b.transfer(blob,manifest,transport=lambda name,data:(_ for _ in ()).throw(OSError('failed')))
    def test_snapshot_with_open_uncommitted_write_is_consistent(self):
        self.db.execute('BEGIN IMMEDIATE');p.event(self.db,'pending',123,{})
        manifest,raw=b.snapshot(self.path,self.root/'concurrent.sqlite',clock=lambda:3);self.assertEqual(manifest['counts']['events'],0)
        self.db.commit();self.assertEqual(self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],1);self.assertEqual(b.verify(b.package(manifest,raw))['counts']['events'],0)
    def test_upload_failure_never_records_success(self):
        work=self.root/'work'
        with patch.object(b,'WORK',work),patch.object(b,'STATE',self.path),patch.object(b,'transfer',side_effect=OSError('offline')):
            with self.assertRaises(OSError):b.run()
        self.assertFalse((work/'success.json').exists())
if __name__=='__main__':unittest.main()
