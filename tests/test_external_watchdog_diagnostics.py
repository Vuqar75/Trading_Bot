import hashlib
import io
import json
import sqlite3
import subprocess
import tarfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_paper_coordinated_backup as backup_fixtures
from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.margin_app import cycle_diagnostic
from trading_bot.paper_external_backup import prepare,inspect_restore,upload
from trading_bot.paper_gcs_transport import upload_verified
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.paper_watchdog import assess,transitions
from trading_bot.paper_watchdog_runtime import publish


class ExternalBackupTests(unittest.TestCase):
    def setUp(self):
        fixture=backup_fixtures.CoordinatedBackupTests('test_busy_queue_does_not_publish_partial_backup_or_leave_financial_lock')
        fixture.setUp();self.addCleanup(fixture.doCleanups);self.f=fixture
        shared=SharedPaperStore(fixture.financial)
        try:CrossPaperBook(shared);MarginPaperBook(shared.ledger)
        finally:shared.close()
        self.stage=fixture.root/'stage';self.stage.mkdir()

    def test_backup_restore_isolated_and_upload_acknowledgement(self):
        p=prepare(self.f.financial,self.f.queue,self.f.observer,self.stage,commit='a'*40)
        restored=inspect_restore(p['path'],self.f.root/'isolated',expected_sha256=p['sha256'])
        self.assertTrue(restored['balanced']);self.assertEqual(restored['commit'],'a'*40)
        for name,src in [('financial.db',self.f.financial),('queue.db',self.f.queue),('observation.db',self.f.observer)]:
            with closing(sqlite3.connect(src)) as a,closing(sqlite3.connect(self.f.root/'isolated'/name)) as b:
                self.assertEqual(list(a.iterdump()),list(b.iterdump()))
        records=[]
        with self.assertRaises(OSError):upload(p,lambda *_:(_ for _ in ()).throw(OSError('unavailable')),records.append)
        self.assertEqual(records,[])
        with self.assertRaises(ValueError):upload(p,lambda *_:{'object':'object','sha256':'bad'},records.append)
        self.assertEqual(records,[])
        upload(p,lambda path,digest:{'object':'private/object','sha256':digest},records.append)
        self.assertEqual(len(records),1)

    def test_corrupt_and_path_traversal_backups_rejected(self):
        p=prepare(self.f.financial,self.f.queue,self.f.observer,self.stage,commit='b'*40)
        path=self.stage/'bad.tar.gz';path.write_bytes(Path(p['path']).read_bytes()+b'x')
        with self.assertRaisesRegex(ValueError,'checksum'):inspect_restore(path,self.f.root/'bad-restore',expected_sha256=p['sha256'])
        with tarfile.open(path,'w:gz') as tar:
            info=tarfile.TarInfo('../escape');info.size=1;tar.addfile(info,io.BytesIO(b'x'))
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError,'content'):inspect_restore(path,self.f.root/'bad-restore',expected_sha256=digest)
        self.assertFalse((self.f.root/'escape').exists());self.assertFalse((self.f.root/'bad-restore').exists())

    def test_upload_uncertainty_requires_matching_readback(self):
        p=prepare(self.f.financial,self.f.queue,self.f.observer,self.stage,commit='c'*40)
        data=Path(p['path']).read_bytes();stored={}
        def transport(args,**kwargs):
            if '--if-generation-match=0' in args:
                stored[args[-1]]=Path(args[-2]).read_bytes()
                raise subprocess.TimeoutExpired('network',1)
            if args[-2] not in stored:raise subprocess.CalledProcessError(1,'network')
            Path(args[-1]).write_bytes(stored[args[-2]])
        result=upload_verified(p['path'],p['sha256'],bucket='private-bucket-example',kind='hourly',captured_at=p['captured_at'],run=transport)
        self.assertEqual(result['sha256'],p['sha256']);self.assertEqual(next(iter(stored.values())),data)
        stored.clear()
        def no_upload(args,**kwargs):raise subprocess.CalledProcessError(1,'network')
        with self.assertRaisesRegex(ValueError,'unavailable'):
            upload_verified(p['path'],p['sha256'],bucket='private-bucket-example',kind='hourly',captured_at=p['captured_at'],run=no_upload)


class WatchdogTests(unittest.TestCase):
    def healthy(self):
        now=10000
        h={'time':now,'error':None,'blocked':None,'paused':True,'pause_source':'manual','quote':{'time':now},'quotes_fresh':True,'protection_fresh':True,'protection_checked_at':now*1000}
        health={x:dict(h) for x in ('spot','futures','cross','margin')};health['cross']['protection_checked_at']=now
        return {'services':{x:'active' for x in ('spot','futures','cross','margin','monitor','backup_timer')},
                'health':health,'queue':{'pending':0},
                'backups':{'local':now,'external':now},'resources':{'memory_available_bytes':200*1024*1024,'disk_free_bytes':600*1024*1024}}

    def test_failure_suppression_recovery_and_pause(self):
        sample=self.healthy();self.assertEqual(assess(sample,10000),{})
        sample['health']['margin']['time']=9800;sample['backups']['external']=None
        state,events=transitions(assess(sample,10000),{},10000)
        self.assertEqual({e['key'] for e in events},{'health:margin','backup:external'})
        state,events=transitions(assess(sample,10001),state,10001);self.assertEqual(events,[])
        state,events=transitions(assess(self.healthy(),10002),state,10002)
        self.assertEqual({e['kind'] for e in events},{'recovered'});self.assertEqual(state,{})

    def test_absent_protection_and_stuck_queue_are_not_healthy(self):
        s=self.healthy();s['health']['cross']['protection_checked_at']=None;s['queue']={'pending':1,'oldest_received':9000}
        s['health']['margin']['pause_source']='protective';s['health']['cross']['pause_source']='unknown'
        r=assess(s,10000);self.assertIn('protection:cross',r);self.assertIn('queue:stuck',r)
        self.assertIn('защитная',r['pause:margin']['message']);self.assertIn('не подтверждена',r['pause:cross']['message'])

    def test_diagnostics_does_not_copy_sensitive_error_text_or_url(self):
        try:raise ValueError('token=super-secret https://example.com/?signature=secret')
        except ValueError as exc:d=cycle_diagnostic(exc,'fetch','BTCUSDT','15m')
        output=json.dumps(d);self.assertEqual(d['stage'],'fetch');self.assertEqual(d['exception_type'],'ValueError')
        self.assertEqual(d['symbol'],'BTCUSDT');self.assertNotIn('secret',output);self.assertNotIn('https',output)

    def test_outbox_transaction_deduplicates_and_reports_recovery(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'financial.db'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE futures_outbox(id INTEGER PRIMARY KEY,message TEXT NOT NULL)');db.commit()
            broken=self.healthy();broken['health']['margin']['time']=9000
            self.assertEqual(publish(path,broken,now=10000)['new_messages'],1)
            self.assertEqual(publish(path,broken,now=10001)['new_messages'],0)
            self.assertEqual(publish(path,self.healthy(),now=10002)['new_messages'],1)
            with closing(sqlite3.connect(path)) as db:
                messages=[r[0] for r in db.execute('SELECT message FROM futures_outbox ORDER BY id')]
                self.assertEqual(len(messages),2);self.assertIn('ВОССТАНОВЛЕНО',messages[-1])
                db.execute("CREATE TRIGGER reject_outbox BEFORE INSERT ON futures_outbox BEGIN SELECT RAISE(ABORT,'fail'); END");db.commit()
            with self.assertRaises(sqlite3.IntegrityError):publish(path,broken,now=10003)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(json.loads(db.execute("SELECT value FROM paper_watchdog_state WHERE key='incidents'").fetchone()[0]),{})

    def test_three_queue_samples_detect_growth_without_old_commands(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'financial.db'
            with closing(sqlite3.connect(path)) as db:db.execute('CREATE TABLE futures_outbox(id INTEGER PRIMARY KEY,message TEXT NOT NULL)');db.commit()
            for count in (1,2,3):
                sample=self.healthy();sample['queue']={'pending':count,'oldest_received':10000}
                publish(path,sample,now=10000+count)
            with closing(sqlite3.connect(path)) as db:
                messages=[r[0] for r in db.execute('SELECT message FROM futures_outbox')]
            self.assertEqual(len(messages),1);self.assertIn('растёт',messages[0])
