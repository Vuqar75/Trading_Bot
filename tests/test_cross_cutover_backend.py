"""End-to-end backend with real SQLite/files and a fake service controller."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from trading_bot.cross_cutover_backend import CrossCutoverBackend
from trading_bot.cross_cutover_journal import cutover_journal
from trading_bot.cross_cutover_machine import run_cutover_phases
from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_coordinated_backup import coordinated_backup
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue


class FakeController:
    def __init__(self,shared,unit_dir,candidate,marker,names):
        self.shared=shared;self.units=unit_dir;self.candidate=candidate;self.marker=marker;self.names=names
        self.running=True;self.enabled=False;self.boot='boot-a';self.stray=False;self.backups=unit_dir.parent/'live-backups';self.backups.mkdir();self.count=0
    def interlocked_services(self):return self.names
    def reload_units(self):return True
    def interlock_loaded(self,marker):
        text='ConditionPathExists=!'+str(marker)
        return all((self.units/(n+'.d')/'90-paper-cross-interlock.conf').is_file() and text in (self.units/(n+'.d')/'90-paper-cross-interlock.conf').read_text() for n in self.names)
    def stop_all(self):self.running=False
    def stopped(self):return not self.running and not self.stray
    def start_all(self):
        if self.marker.exists():raise ValueError('interlock active')
        self.running=True
    def start_old_paused(self):self.start_all()
    def enable_cross(self):self.enabled=True
    def disable_cross(self):self.enabled=False
    def loaded_units_match(self,unit_dir,units):return all((unit_dir/n).is_file() and (unit_dir/n).read_bytes()==(self.candidate/n).read_bytes() for n in units)
    def old_units_loaded(self,unit_dir,units,new_units):return all(not (unit_dir/n).exists() if n in new_units else (unit_dir/n).read_text()=='old '+n for n in units)
    def wait_healthy_paused(self):
        return self.running and self.shared.deployment_hold() and all(self.shared._get(m,'paused') is True for m in ('spot','futures')) and self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0]==1
    def wait_healthy_old_paused(self):return self.running and self.shared.deployment_hold() and all(self.shared._get(m,'paused') is True for m in ('spot','futures'))
    def wait_healthy_after_release(self):return self.running and not self.shared.deployment_hold() and self.shared.ledger.reconcile()['balanced']
    def boot_id(self):return self.boot
    def backup_root(self):return self.backups
    def live_backup(self):
        self.count+=1;target=self.backups/('backup-'+str(self.count))
        coordinated_backup(self.financial,self.queue,self.observer,target,timeout=10)
        return {'path':str(target),'min_available_kib':500000,'samples':2}


class CrossCutoverBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);r=Path(self.tmp.name);self.root=r
        spot=r/'spot.db';future=r/'future.db';financial=r/'financial.db'
        s=RuntimeStore(spot,{'mode':'paper','interval':'15m'});f=FuturesStore(future)
        s.command(1,True,'/resume');f.command(1,True,'/resume');f.open_position('SHORT',Decimal(100),1000,'entry')
        self.position=f.get('position');s.command(2,True,'/pause');f.command(2,True,'/pause');s.close();f.close()
        consolidate_snapshots(spot,future,financial,captured_at=1)
        self.shared=SharedPaperStore(financial);self.q=UnifiedQueue(r/'queue.db');self.addCleanup(self.shared.close);self.addCleanup(self.q.close)
        with self.shared.transaction():
            self.shared._set('spot','paused',False);self.shared._set('futures','paused',False)
            self.shared._set('spot','quote',{'price':'100','time':100})
            self.shared._set('futures','quote',{'price':'100','time':100,'server_ms':1000})
        book=CrossPaperBook(self.shared)
        quotes={s:{'mark':'100','time':100,'funding_complete':True,'server_ms':100000,'next_due_ms':28000000,'interval_ms':28800000} for s in ('BTCUSDT','ETHUSDT')}
        book.record_quotes(quotes,now=100)
        self.obs=r/'observation.db'
        with closing(sqlite3.connect(self.obs)) as db:db.execute('CREATE TABLE samples(ts REAL PRIMARY KEY,equity TEXT)');db.commit()
        self.units=r/'units';self.candidate=r/'candidate';self.archive=r/'archive';self.journal=r/'journal'
        for d in (self.units,self.candidate,self.archive,self.journal):d.mkdir(mode=0o700)
        self.names=('spot.service','futures.service','cross.service');self.marker=r/'interlock';self.operation='cross:freeze:backend'
        for n in self.names:
            (self.candidate/n).write_text('new '+n)
            if n!='cross.service':(self.units/n).write_text('old '+n)
        self.controller=FakeController(self.shared,self.units,self.candidate,self.marker,self.names)
        self.controller.financial=financial;self.controller.queue=r/'queue.db';self.controller.observer=self.obs
        self.backend=CrossCutoverBackend(self.shared,self.q,self.obs,self.units,self.candidate,self.archive,self.marker,
                                         self.names,('cross.service',),self.operation,self.controller)

    def test_end_to_end_with_partial_switch_and_refused_financial_rollback(self):
        fired=[]
        def crash(point):
            if point=='switched:spot.service' and not fired:fired.append(point);raise RuntimeError('unit crash')
        self.backend.fault=crash
        with patch('time.time',return_value=100),cutover_journal(self.journal,self.operation,'a'*40) as journal:
            with self.assertRaisesRegex(RuntimeError,'unit crash'):
                run_cutover_phases(journal,self.backend.actions(),self.backend.probes())
            self.assertEqual(journal.state['intent'],'units_switched');self.assertTrue(self.marker.exists())
            self.assertTrue(self.controller.stopped());self.backend.fault=lambda p:None
            self.assertEqual(run_cutover_phases(journal,self.backend.actions(),self.backend.probes()),'awaiting_reboot')
            self.assertTrue(self.shared.deployment_hold());self.assertEqual(self.shared.ledger.balance('futures_cross:free'),Decimal(50))
            self.assertEqual(self.shared.snapshot('futures')['position'],self.position)
            with self.assertRaisesRegex(ValueError,'Rollback safety not proven'):
                self.backend.rollback_old_code_paused()
            self.assertTrue(self.marker.exists());self.assertTrue(self.controller.stopped())
            self.backend._start_paused();self.controller.boot='boot-b'
            self.assertEqual(run_cutover_phases(journal,self.backend.actions(),self.backend.probes(),after_reboot=True,verify_reboot=self.backend.reboot_verified),'complete')
        self.assertEqual(self.shared.snapshot('futures')['position'],self.position)
        self.assertEqual(self.shared.db.execute("SELECT count(*) FROM ledger_events WHERE kind='TRANSFER'").fetchone()[0],1)
        self.assertFalse(self.shared.deployment_hold());self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_before_transfer_can_restore_old_unit_files_without_restoring_databases(self):
        with patch('time.time',return_value=100):
            self.backend._arm();self.backend._freeze();self.backend._stop();self.backend._backup(self.backend.before);self.backend._switch()
            self.backend.rollback_old_code_paused()
        self.assertTrue(self.controller.running);self.assertTrue(self.shared.deployment_hold());self.assertFalse(self.controller.enabled)
        self.assertEqual((self.units/'spot.service').read_text(),'old spot.service');self.assertFalse((self.units/'cross.service').exists())
        self.assertEqual(self.shared.snapshot('futures')['position'],self.position)
        self.assertEqual(self.shared.db.execute("SELECT count(*) FROM ledger_events WHERE kind='TRANSFER'").fetchone()[0],0)

    def test_health_failure_after_release_restores_hold_without_closing_position(self):
        with patch('time.time',return_value=100),cutover_journal(self.journal,self.operation,'a'*40) as journal:
            self.assertEqual(run_cutover_phases(journal,self.backend.actions(),self.backend.probes()),'awaiting_reboot')
            self.controller.boot='boot-b';self.controller.wait_healthy_after_release=lambda:False
            with self.assertRaisesRegex(ValueError,'health after release'):
                run_cutover_phases(journal,self.backend.actions(),self.backend.probes(),after_reboot=True,verify_reboot=self.backend.reboot_verified)
            self.assertEqual(journal.state['intent'],'complete')
            self.assertTrue(self.shared.deployment_hold())
            self.assertTrue(all(self.shared.snapshot(m)['paused'] for m in ('spot','futures')))
            self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0],1)
            self.assertEqual(self.shared.snapshot('futures')['position'],self.position)
