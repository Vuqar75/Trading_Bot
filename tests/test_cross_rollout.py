import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import shutil
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.cross_rollout import allocate_initial_cross,freeze_entries,release_entries_after_cutover
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue
from trading_bot.paper_wallet_live_report import live_report
from trading_bot.paper_coordinated_backup import coordinated_backup
from trading_bot.cross_snapshot_guard import financial_history_unchanged
from trading_bot.cross_backup_verifier import verify_restorable_backup


class CrossRolloutTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        spot=root/'spot.db';future=root/'future.db';self.path=root/'financial.db';self.qpath=root/'queue.db'
        a=RuntimeStore(spot,{'mode':'paper','interval':'15m'});b=FuturesStore(future)
        b.command(1,True,'/resume');b.open_position('SHORT',Decimal(100),1000,'existing-position')
        self.existing_position=b.get('position')
        with b.transaction():b.set('paused',True)
        a.close();b.close()
        consolidate_snapshots(spot,future,self.path,captured_at=1)
        self.shared=SharedPaperStore(self.path);self.addCleanup(self.shared.close)
        with self.shared.transaction():
            self.shared._set('spot','quote',{'price':'100','time':100})
            self.shared._set('futures','quote',{'price':'100','time':100,'server_ms':1000})
        self.book=CrossPaperBook(self.shared)
        quotes={s:{'mark':'100','time':100,'funding_complete':True,'server_ms':100000,'next_due_ms':28000000,'interval_ms':28800000} for s in ('BTCUSDT','ETHUSDT')}
        self.book.record_quotes(quotes,now=100)
        self.shared.set_deployment_hold(True)
        self.q=UnifiedQueue(self.qpath);self.addCleanup(self.q.close)

    def allocate(self):
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):
            return allocate_initial_cross(self.shared,self.q,'cross:initial:fixture',now=100)

    def prepare_freeze(self):
        with self.shared.transaction():
            self.shared.db.execute("DELETE FROM ledger_meta WHERE key='deployment_hold'")
            self.shared._set('spot','paused',False);self.shared._set('futures','paused',True)

    def test_freeze_atomically_remembers_pause_and_replays_after_crash(self):
        self.prepare_freeze()
        code=("import os,sys; from trading_bot.paper_shared_store import SharedPaperStore; "
              "from trading_bot.unified_queue import UnifiedQueue; from trading_bot.cross_rollout import freeze_entries; "
              "s=SharedPaperStore(sys.argv[1]); q=UnifiedQueue(sys.argv[2]); "
              "s.fault=lambda p:os._exit(79) if p==sys.argv[3] else None; "
              "freeze_entries(s,q,'cross:freeze:fixture',now=100)")
        for point in ('before_commit','after_commit'):
            child=subprocess.run([sys.executable,'-c',code,str(self.path),str(self.qpath),point])
            self.assertEqual(child.returncode,79)
            done=point=='after_commit'
            self.assertEqual(self.shared.deployment_hold(),done)
            self.assertEqual(self.shared.snapshot('spot')['paused'],done)
            self.assertTrue(self.shared.snapshot('futures')['paused'])
        result=freeze_entries(self.shared,self.q,'cross:freeze:fixture',now=101)
        self.assertTrue(result['replayed']);self.assertEqual(result['paused'],{'spot':False,'futures':True})
        self.assertEqual(result['queue_offset'],0)
        self.assertEqual(self.shared.snapshot('futures')['position'],self.existing_position)

    def test_freeze_rejects_pending_commands_without_changing_pauses(self):
        self.prepare_freeze();self.q.receive(1,True,'/pause',now=100)
        with self.assertRaisesRegex(ValueError,'Pending'):freeze_entries(self.shared,self.q,'cross:freeze:fixture',now=100)
        self.assertFalse(self.shared.deployment_hold());self.assertFalse(self.shared.snapshot('spot')['paused'])

    def test_freeze_rechecks_current_quote_age_before_committing_hold(self):
        self.prepare_freeze()
        with patch('trading_bot.cross_rollout.time.time',return_value=221):
            with self.assertRaisesRegex(ValueError,'stale'):
                freeze_entries(self.shared,self.q,'cross:freeze:fixture')
        self.assertFalse(self.shared.deployment_hold());self.assertFalse(self.shared.snapshot('spot')['paused'])

    def test_freeze_serializes_receipt_until_pause_commit(self):
        self.prepare_freeze();ready=threading.Event();attempt=threading.Event();out=[]
        def receive():
            other=UnifiedQueue(self.qpath)
            try:
                other.db.execute('PRAGMA busy_timeout=20');ready.set();attempt.wait(5)
                try:other.receive(7,True,'/pause',now=100);out.append('unexpected')
                except sqlite3.OperationalError:out.append('locked')
            finally:other.close()
        worker=threading.Thread(target=receive);worker.start();self.assertTrue(ready.wait(5))
        def fault(point):
            if point=='before_commit':attempt.set();worker.join(5)
        self.shared.fault=fault
        try:result=freeze_entries(self.shared,self.q,'cross:freeze:fixture',now=100)
        finally:self.shared.fault=lambda point:None
        worker.join(5);self.assertEqual(out,['locked']);self.assertEqual(result['queue_offset'],0)
        self.q.receive(7,True,'/pause',now=101)
        self.assertEqual(self.q.pending('spot')[0]['update_id'],7)
        self.assertTrue(self.shared.deployment_hold())

    def test_release_respects_original_pauses_and_never_reactivates_after_restart(self):
        self.prepare_freeze();freeze_entries(self.shared,self.q,'cross:freeze:fixture',now=100);self.allocate()
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):
            result=release_entries_after_cutover(self.shared,self.q,'cross:release:fixture',now=100)
        self.assertEqual(result['enabled'],['spot','futures_cross'])
        self.assertFalse(self.shared.snapshot('spot')['paused']);self.assertTrue(self.shared.snapshot('futures')['paused'])
        with self.shared.transaction():
            self.shared._set('spot','paused',True)
            self.shared.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='futures_cross'")
        result=release_entries_after_cutover(self.shared,self.q,'cross:release:fixture',now=101)
        self.assertTrue(result['replayed']);self.assertTrue(self.shared.snapshot('spot')['paused'])
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0],1)

    def test_release_respects_pause_all_after_freeze(self):
        self.prepare_freeze();freeze_entries(self.shared,self.q,'cross:freeze:fixture',now=100);self.allocate()
        self.q.receive(9,True,'/pause_all',now=100)
        for market in ('spot','futures','futures_cross'):
            for job in self.q.pending(market):self.q.acknowledge(job['id'],'paused')
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):
            result=release_entries_after_cutover(self.shared,self.q,'cross:release:fixture',now=100)
        self.assertEqual(result['enabled'],[])
        self.assertEqual(result['manual_pauses'],['futures','futures_cross','spot'])

    def test_release_crash_before_and_after_commit_is_idempotent(self):
        self.prepare_freeze();freeze_entries(self.shared,self.q,'cross:freeze:fixture',now=100);self.allocate()
        code=("import os,sys; from unittest.mock import patch; from trading_bot.paper_shared_store import SharedPaperStore; "
              "from trading_bot.unified_queue import UnifiedQueue; from trading_bot.cross_rollout import release_entries_after_cutover; "
              "s=SharedPaperStore(sys.argv[1]); q=UnifiedQueue(sys.argv[2]); "
              "s.fault=lambda p:os._exit(79) if p==sys.argv[3] else None; "
              "p=patch('trading_bot.paper_shared_store.time.time',return_value=100); p.start(); "
              "release_entries_after_cutover(s,q,'cross:release:fixture',now=100)")
        for point in ('before_commit','after_commit'):
            child=subprocess.run([sys.executable,'-c',code,str(self.path),str(self.qpath),point])
            self.assertEqual(child.returncode,79)
            self.assertEqual(self.shared.snapshot('spot')['paused'],point=='before_commit')
        with self.shared.transaction():self.shared._set('spot','paused',True)
        self.assertTrue(release_entries_after_cutover(self.shared,self.q,'cross:release:fixture',now=101)['replayed'])
        self.assertTrue(self.shared.snapshot('spot')['paused'])

    def test_release_refuses_missing_or_conflicting_allocation(self):
        self.prepare_freeze();freeze_entries(self.shared,self.q,'cross:freeze:fixture',now=100)
        with self.assertRaisesRegex(ValueError,'allocation required'):
            release_entries_after_cutover(self.shared,self.q,'cross:release:fixture',now=100)
        self.allocate()
        with self.shared.transaction():
            row=self.shared.db.execute("SELECT value FROM ledger_meta WHERE key='cross_cutover_allocation'").fetchone()
            data=json.loads(row[0]);data['event']='nonexistent'
            self.shared.db.execute("UPDATE ledger_meta SET value=? WHERE key='cross_cutover_allocation'",(json.dumps(data),))
        with self.assertRaisesRegex(ValueError,'allocation disagrees'):
            release_entries_after_cutover(self.shared,self.q,'cross:release:fixture',now=100)
        self.assertTrue(self.shared.snapshot('spot')['paused'])

    def test_release_rechecks_time_after_ledger_validation(self):
        self.prepare_freeze();freeze_entries(self.shared,self.q,'cross:freeze:fixture',now=100);self.allocate()
        with patch('trading_bot.cross_rollout.time.time',side_effect=[100,100,100,221]):
            with self.assertRaises(ValueError):release_entries_after_cutover(self.shared,self.q,'cross:release:fixture')
        self.assertTrue(self.shared.snapshot('spot')['paused'])
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0],1)

    def test_release_refuses_transfer_event_with_wrong_postings(self):
        self.prepare_freeze();freeze_entries(self.shared,self.q,'cross:freeze:fixture',now=100);self.allocate()
        with self.shared.transaction():
            self.shared.ledger._post('fake:transfer','TRANSFER',{'source':'futures_isolated','destination':'futures_cross','quantity':'50'},
                                     [('external:a','USDT','-50'),('external:b','USDT','50')],created=100)
            row=self.shared.db.execute("SELECT value FROM ledger_meta WHERE key='cross_cutover_allocation'").fetchone()
            marker=json.loads(row[0]);marker['event']='fake:transfer'
            self.shared.db.execute("UPDATE transfer_proposals SET event_id='fake:transfer' WHERE id=?",(marker['token'],))
            self.shared.db.execute("UPDATE ledger_meta SET value=? WHERE key='cross_cutover_allocation'",(json.dumps(marker),))
        with self.assertRaisesRegex(ValueError,'postings disagree'):
            release_entries_after_cutover(self.shared,self.q,'cross:release:fixture',now=100)
        self.assertTrue(self.shared.snapshot('spot')['paused'])

    def test_release_rejects_deleted_pre_freeze_update_even_if_offset_is_unchanged(self):
        self.prepare_freeze();self.q.receive(5,True,'/mode spot',now=100)
        freeze_entries(self.shared,self.q,'cross:freeze:fixture',now=100);self.allocate()
        self.q.db.execute('DELETE FROM unified_updates WHERE id=5')
        with self.assertRaisesRegex(ValueError,'Queue history changed'):
            release_entries_after_cutover(self.shared,self.q,'cross:release:fixture',now=100)
        self.assertTrue(self.shared.snapshot('spot')['paused'])

    def test_allocation_is_atomic_exact_and_idempotent(self):
        before=live_report(self.shared,now=100)
        before_pnl=(before['spot_realized'],before['futures_realized'],before['futures_open'])
        result=self.allocate();self.assertFalse(result['replayed'])
        self.assertEqual(self.shared.ledger.balance('futures_isolated:free'),Decimal(940))
        self.assertEqual(self.shared.ledger.balance('futures_cross:free'),Decimal(50))
        self.assertEqual(self.shared.snapshot('futures')['position'],self.existing_position)
        self.assertEqual(self.shared.ledger.balance('futures_isolated:margin'),Decimal(10))
        self.assertFalse(self.shared.deployment_hold())
        self.assertTrue(self.allocate()['replayed'])
        self.assertEqual(self.shared.db.execute("SELECT count(*) FROM ledger_events WHERE kind='TRANSFER'").fetchone()[0],1)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])
        after=live_report(self.shared,now=100)
        self.assertEqual(before['total'],after['total'])
        self.assertEqual(before_pnl,(after['spot_realized'],after['futures_realized'],after['futures_open']))

    def test_pending_command_or_insufficient_funds_keeps_hold(self):
        self.q.receive(1,True,'/pause',now=100)
        with self.assertRaisesRegex(ValueError,'Pending'):self.allocate()
        self.assertTrue(self.shared.deployment_hold())
        self.q.acknowledge(self.q.pending('spot')[0]['id'],'paused')
        self.shared.apply('futures','lower-cash',{'test':True},lambda db:self.shared._set('futures','cash','10'))
        with self.assertRaisesRegex(ValueError,'Insufficient'):self.allocate()
        self.assertTrue(self.shared.deployment_hold());self.assertEqual(self.shared.ledger.balance('futures_cross:free'),0)

    def test_incomplete_isolated_funding_blocks_allocation(self):
        with self.shared.transaction():self.shared._set('futures','quote',{'price':'100','time':100,'server_ms':1001})
        with self.assertRaisesRegex(ValueError,'funding'):self.allocate()
        self.assertTrue(self.shared.deployment_hold())
        self.assertEqual(self.shared.snapshot('futures')['position'],self.existing_position)
        self.assertEqual(self.shared.ledger.balance('futures_cross:free'),0)

    def test_unpaused_spot_does_not_release_hold_or_transfer(self):
        with self.shared.transaction():self.shared._set('spot','paused',False)
        with self.assertRaisesRegex(ValueError,'Both markets must be paused'):
            self.allocate()
        self.assertTrue(self.shared.deployment_hold())
        self.assertEqual(self.shared.ledger.balance('futures_cross:free'),0)
        self.assertEqual(self.shared.db.execute("SELECT count(*) FROM ledger_events WHERE kind='TRANSFER'").fetchone()[0],0)

    def test_coordinated_copies_and_restore_do_not_overwrite_new_history(self):
        root=self.path.parent;observation=root/'observation.db'
        with closing(sqlite3.connect(observation)) as db:
            db.execute('CREATE TABLE samples(ts REAL PRIMARY KEY,equity TEXT)')
            db.execute('INSERT INTO samples VALUES(?,?)',(100,'1999'))
            db.commit()
        coordinated_backup(self.path,self.qpath,observation,root/'before',timeout=10)
        self.allocate()
        coordinated_backup(self.path,self.qpath,observation,root/'after',timeout=10)
        self.assertFalse(financial_history_unchanged(root/'before'/'financial.db',root/'after'/'financial.db'))
        restored=root/'restored';shutil.copytree(root/'after',restored)
        for name in ('financial.db','queue.db','observation.db'):
            with closing(sqlite3.connect(restored/name)) as db:self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        with closing(SharedPaperStore(restored/'financial.db')) as recovered:
            self.assertTrue(recovered.ledger.reconcile()['balanced'])
            self.assertEqual(recovered.ledger.balance('futures_cross:free'),Decimal(50))
            self.assertEqual(recovered.snapshot('futures')['position'],self.existing_position)
            self.assertEqual(live_report(recovered,now=100)['total'],live_report(self.shared,now=100)['total'])

    def test_verified_frozen_backup_restores_three_databases_and_checks_hashes(self):
        self.prepare_freeze();freeze_entries(self.shared,self.q,'cross:freeze:fixture',now=100)
        root=self.path.parent;observation=root/'observation.db'
        with closing(sqlite3.connect(observation)) as db:
            db.execute('CREATE TABLE samples(ts REAL PRIMARY KEY,equity TEXT)');db.commit()
        backup=root/'backup'
        coordinated_backup(self.path,self.qpath,observation,backup,timeout=10)
        result=verify_restorable_backup(backup,freeze_id='cross:freeze:fixture',scratch_parent=root)
        self.assertTrue(result['ledger_balanced']);self.assertEqual(result['pending_commands'],0)
        self.assertEqual(list(root.glob('cross-restore-check-*')),[])
        with self.assertRaisesRegex(ValueError,'expected paused freeze'):
            verify_restorable_backup(backup,freeze_id='cross:freeze:other',scratch_parent=root)
        with (backup/'observation.db').open('ab') as stream:stream.write(b'changed')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):
            verify_restorable_backup(backup,freeze_id='cross:freeze:fixture',scratch_parent=root)
        self.assertTrue(self.shared.deployment_hold())


    def test_process_death_before_and_after_financial_commit(self):
        code=("import os,sys; from unittest.mock import patch; from trading_bot.paper_shared_store import SharedPaperStore; "
              "from trading_bot.unified_queue import UnifiedQueue; from trading_bot.cross_rollout import allocate_initial_cross; "
              "s=SharedPaperStore(sys.argv[1]); q=UnifiedQueue(sys.argv[2]); "
              "s.fault=lambda p:os._exit(79) if p==sys.argv[3] else None; "
              "p=patch('trading_bot.paper_shared_store.time.time',return_value=100); p.start(); "
              "allocate_initial_cross(s,q,'cross:initial:fixture',now=100)")
        for point in ('before_commit','after_commit'):
            child=subprocess.run([sys.executable,'-c',code,str(self.path),str(self.qpath),point])
            self.assertEqual(child.returncode,79)
            self.assertEqual(self.shared.ledger.balance('futures_cross:free'),Decimal(50 if point=='after_commit' else 0))
            self.assertEqual(self.shared.deployment_hold(),point=='before_commit')
            self.assertTrue(self.shared.ledger.reconcile()['balanced'])
        self.assertTrue(self.allocate()['replayed'])
        self.assertEqual(self.shared.db.execute("SELECT count(*) FROM ledger_events WHERE kind='TRANSFER'").fetchone()[0],1)

    def test_command_receipt_waits_for_financial_commit_and_can_retry(self):
        ready=threading.Event();attempt=threading.Event();out=[]
        def receive():
            other=UnifiedQueue(self.qpath)
            try:
                other.db.execute('PRAGMA busy_timeout=20');ready.set();attempt.wait(5)
                try:other.receive(7,True,'/pause',now=100);out.append('unexpected')
                except sqlite3.OperationalError:out.append('locked')
            finally:other.close()
        worker=threading.Thread(target=receive);worker.start();self.assertTrue(ready.wait(5))
        def fault(point):
            if point=='before_commit':attempt.set();worker.join(5)
        self.shared.fault=fault
        try:self.allocate()
        finally:self.shared.fault=lambda point:None
        worker.join(5);self.assertEqual(out,['locked']);self.assertEqual(self.q.state('offset'),'0')
        self.q.receive(7,True,'/pause',now=101)
        self.assertEqual(self.q.pending('spot')[0]['update_id'],7)
