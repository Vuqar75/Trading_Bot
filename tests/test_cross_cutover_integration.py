"""Staged cross cutover on isolated files, with a fake service manager."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from trading_bot.cross_backup_verifier import verify_restorable_backup
from trading_bot.cross_cutover_journal import cutover_journal
from trading_bot.cross_cutover_machine import run_cutover_phases
from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.cross_rollout import freeze_entries,release_entries_after_cutover
from trading_bot.cross_verified_allocation import allocate_verified_cross
from trading_bot.cross_migration_verifier import verify_same_price_migration
from trading_bot.cross_service_interlock import arm_interlock,release_interlock
from trading_bot.cross_snapshot_guard import financial_history_unchanged
from trading_bot.cross_unit_switch import hold_writer_locks,switch_unit_files,restore_unit_files
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_coordinated_backup import coordinated_backup
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.paper_wallet_live_report import live_report
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue


class CrossCutoverIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        spot=self.root/'spot.db';future=self.root/'future.db';self.financial=self.root/'financial.db'
        s=RuntimeStore(spot,{'mode':'paper','interval':'15m'});f=FuturesStore(future)
        s.command(1,True,'/resume');f.command(1,True,'/resume');f.open_position('SHORT',Decimal(100),1000,'entry')
        self.position=f.get('position');s.command(2,True,'/pause');f.command(2,True,'/pause')
        s.close();f.close();consolidate_snapshots(spot,future,self.financial,captured_at=1)
        self.shared=SharedPaperStore(self.financial);self.q=UnifiedQueue(self.root/'queue.db')
        self.addCleanup(self.shared.close);self.addCleanup(self.q.close)
        with self.shared.transaction():
            self.shared._set('spot','paused',False);self.shared._set('futures','paused',False)
            self.shared._set('spot','quote',{'price':'100','time':100})
            self.shared._set('futures','quote',{'price':'100','time':100,'server_ms':1000})
        self.book=CrossPaperBook(self.shared)
        quotes={symbol:{'mark':'100','time':100,'funding_complete':True,'server_ms':100000,'next_due_ms':28000000,'interval_ms':28800000} for symbol in ('BTCUSDT','ETHUSDT')}
        self.book.record_quotes(quotes,now=100)
        self.obs=self.root/'observation.db'
        with closing(sqlite3.connect(self.obs)) as db:db.execute('CREATE TABLE samples(ts REAL PRIMARY KEY,equity TEXT)');db.commit()
        self.units=self.root/'units';self.candidate=self.root/'candidate';self.archive=self.root/'archive';self.journal=self.root/'journal'
        for directory in (self.units,self.candidate,self.archive,self.journal):directory.mkdir(mode=0o700)
        self.names=('spot.service','futures.service','cross.service');self.marker=self.root/'interlock'
        for name in self.names:
            (self.candidate/name).write_text('new '+name)
            if name!='cross.service':(self.units/name).write_text('old '+name)

    def test_partial_unit_switch_transfer_reboot_and_no_snapshot_overwrite(self):
        r=self.root;before=r/'before';after=r/'after';op='cross:freeze:integration';released='cross:release:integration';allocation='cross:initial:integration'
        marker=lambda name:r/('phase-'+name)
        def file_action(name):return lambda:marker(name).write_text('done')
        def file_probe(name):return lambda:'DONE' if marker(name).exists() else 'NOT_STARTED'
        def arm():arm_interlock(self.units,self.marker,self.names,op,reload_units=lambda:True)
        def freeze():freeze_entries(self.shared,self.q,op,now=100)
        def backup():
            coordinated_backup(self.financial,r/'queue.db',self.obs,before,timeout=10)
            self.assertEqual(verify_restorable_backup(before,freeze_id=op,scratch_parent=r)['pending_commands'],0)
        def switch():
            self.assertTrue(self.marker.exists())
            def crash(point):
                if point=='switched:spot.service' and not marker('fault-fired').exists():
                    marker('fault-fired').write_text('done');raise RuntimeError('first unit crash')
            with hold_writer_locks(self.financial):
                switch_unit_files(self.units,self.candidate,self.archive,self.names,new_units=('cross.service',),assert_safe=lambda:True,fault=crash)
        def switched():
            return 'DONE' if all((self.units/n).is_file() and (self.units/n).read_bytes()==(self.candidate/n).read_bytes() for n in self.names) else 'NOT_STARTED'
        def start_paused():
            self.assertTrue(self.shared.deployment_hold());self.assertTrue(all(self.shared.snapshot(m)['paused'] for m in ('spot','futures')))
            release_interlock(self.units,self.marker,self.names,op,verify_safe=lambda:True);file_action('started_paused')()
        def allocate():
            previous=live_report(self.shared,now=100)
            with patch('trading_bot.paper_shared_store.time.time',return_value=100):
                allocate_verified_cross(self.shared,self.q,allocation,assert_quiescent=lambda:True,now=100)
            self.assertTrue(self.shared.deployment_hold())
            current=live_report(self.shared,now=100)
            self.assertEqual(previous['total'],current['total']);self.assertEqual(previous['futures_realized'],current['futures_realized'])
            self.assertEqual(self.shared.snapshot('futures')['position'],self.position)
        def backup_after():
            coordinated_backup(self.financial,r/'queue.db',self.obs,after,timeout=10)
            self.assertFalse(financial_history_unchanged(before/'financial.db',after/'financial.db'))
            reconciliation=verify_same_price_migration(before/'financial.db',after/'financial.db',scratch_parent=r)
            self.assertEqual(reconciliation['capital_before'],reconciliation['capital_after'])
        def release():
            with patch('trading_bot.paper_shared_store.time.time',return_value=100):release_entries_after_cutover(self.shared,self.q,released,now=100)
        actions={'interlock_armed':arm,'frozen':freeze,'stopped':file_action('stopped'),'backup_verified':backup,
                 'units_switched':switch,'started_paused':start_paused,'allocated':allocate,'backup_after':backup_after,
                 'awaiting_reboot':file_action('awaiting_reboot'),'released':release,'complete':file_action('complete')}
        probes={'interlock_armed':lambda:'DONE' if self.marker.exists() else 'NOT_STARTED',
                'frozen':lambda:'DONE' if self.shared.deployment_hold() else 'NOT_STARTED',
                'backup_verified':lambda:'DONE' if before.exists() else 'NOT_STARTED','units_switched':switched,
                'allocated':lambda:'DONE' if self.shared.db.execute("SELECT 1 FROM ledger_meta WHERE key='cross_allocation_verified'").fetchone() else 'NOT_STARTED',
                'backup_after':lambda:'DONE' if after.exists() else 'NOT_STARTED',
                'released':lambda:'DONE' if self.shared.db.execute("SELECT 1 FROM ledger_meta WHERE key='cross_cutover_release'").fetchone() else 'NOT_STARTED'}
        for stage in ('stopped','started_paused','awaiting_reboot','complete'):probes[stage]=file_probe(stage)
        with cutover_journal(self.journal,op,'a'*40) as journal:
            with self.assertRaisesRegex(RuntimeError,'first unit crash'):run_cutover_phases(journal,actions,probes)
            self.assertEqual(journal.state['intent'],'units_switched');self.assertTrue(self.marker.exists())
            def crash_after_allocation(point):
                if point=='after_action:allocated':raise RuntimeError('allocation checkpoint crash')
            with self.assertRaisesRegex(RuntimeError,'allocation checkpoint crash'):
                run_cutover_phases(journal,actions,probes,fault=crash_after_allocation)
            self.assertEqual(journal.state['intent'],'allocated')
            self.assertEqual(self.shared.db.execute("SELECT count(*) FROM ledger_events WHERE kind='TRANSFER'").fetchone()[0],1)
            self.assertEqual(run_cutover_phases(journal,actions,probes),'awaiting_reboot')
            self.assertTrue(self.shared.deployment_hold())
            self.assertEqual(self.shared.ledger.balance('futures_cross:free'),Decimal(50))
            with self.assertRaisesRegex(ValueError,'Rollback safety not proven'):
                restore_unit_files(self.units,self.archive,self.names,assert_safe=lambda:True,
                    verify_rollback=lambda:financial_history_unchanged(before/'financial.db',after/'financial.db'))
            with self.assertRaisesRegex(ValueError,'Reboot'):
                run_cutover_phases(journal,actions,probes,after_reboot=True,verify_reboot=lambda:False)
            self.assertEqual(run_cutover_phases(journal,actions,probes,after_reboot=True,verify_reboot=lambda:True),'complete')
        self.assertEqual(self.shared.snapshot('futures')['position'],self.position)
        self.assertEqual(self.shared.db.execute("SELECT count(*) FROM ledger_events WHERE kind='TRANSFER'").fetchone()[0],1)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])
