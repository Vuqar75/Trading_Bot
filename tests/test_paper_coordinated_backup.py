import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from trading_bot.app import Application
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_coordinated_backup import coordinated_backup
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.paper_wallet_worker import process_wallet
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue


class CoordinatedBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        spot=RuntimeStore(self.root/'spot.db',{'mode':'paper','interval':'15m'});future=FuturesStore(self.root/'future.db')
        try:
            Application(spot,PaperExchange(),None)
            with future.transaction():future.set('paused',True)
            for src,name in ((spot.db,'spot-copy.db'),(future.db,'future-copy.db')):
                with sqlite3.connect(self.root/name) as dst:src.backup(dst)
        finally:spot.close();future.close()
        self.financial=self.root/'financial.db';self.queue=self.root/'queue.db';self.observer=self.root/'observation.db';self.dest=self.root/'backup'
        consolidate_snapshots(self.root/'spot-copy.db',self.root/'future-copy.db',self.financial,captured_at=1)
        q=UnifiedQueue(self.queue);q.close()
        with sqlite3.connect(self.observer) as db:db.execute('create table samples(ts real primary key)');db.execute('insert into samples values(1)')

    def test_backup_preserves_pending_ack_and_deduplicates_transfer_after_restore(self):
        q=UnifiedQueue(self.queue);s=SharedPaperStore(self.financial)
        try:
            q.receive(10,True,'/transfer spot reserve 20');process_wallet(q,s)
            token=s.db.execute('select id from transfer_proposals').fetchone()[0]
            q.receive(11,True,'/confirm '+token)
            def crash(p):
                if p=='after_destination_commit':raise RuntimeError('injected')
            with self.assertRaises(RuntimeError):process_wallet(q,s,fault=crash)
            self.assertEqual(len(q.pending('wallet')),1)
        finally:q.close();s.close()
        manifest=coordinated_backup(self.financial,self.queue,self.observer,self.dest)
        self.assertEqual(len(manifest['files']),3)
        restored=SharedPaperStore(self.dest/'financial.db');rq=UnifiedQueue(self.dest/'queue.db')
        try:
            process_wallet(rq,restored)
            self.assertFalse(rq.pending('wallet'))
            self.assertEqual(restored.ledger.balance('reserve:free'),20)
            self.assertEqual(restored.db.execute("select count(*) from ledger_events where kind='TRANSFER'").fetchone()[0],1)
            self.assertTrue(restored.ledger.reconcile()['balanced'])
        finally:rq.close();restored.close()

    def test_process_death_before_publication_never_exposes_partial_backup(self):
        code=("import os,sys; from trading_bot.paper_coordinated_backup import coordinated_backup; "
              "coordinated_backup(*sys.argv[1:],fault=lambda p:os._exit(71) if p=='before_publish' else None)")
        result=subprocess.run([sys.executable,'-c',code,str(self.financial),str(self.queue),str(self.observer),str(self.dest)])
        self.assertEqual(result.returncode,71);self.assertFalse(self.dest.exists())
        coordinated_backup(self.financial,self.queue,self.observer,self.dest)
        self.assertTrue((self.dest/'manifest.json').exists())

    def test_process_death_after_publication_leaves_complete_backup(self):
        code=("import os,sys; from trading_bot.paper_coordinated_backup import coordinated_backup; "
              "coordinated_backup(*sys.argv[1:],fault=lambda p:os._exit(72) if p=='after_publish' else None)")
        result=subprocess.run([sys.executable,'-c',code,str(self.financial),str(self.queue),str(self.observer),str(self.dest)])
        self.assertEqual(result.returncode,72);self.assertTrue(self.dest.exists())
        for name in ('financial.db','queue.db','observation.db'):
            with sqlite3.connect(self.dest/name) as db:self.assertEqual(db.execute('pragma integrity_check').fetchone()[0],'ok')
        with self.assertRaises(ValueError):coordinated_backup(self.financial,self.queue,self.observer,self.dest)

    def test_busy_queue_does_not_publish_partial_backup_or_leave_financial_lock(self):
        blocker=sqlite3.connect(self.queue,isolation_level=None);blocker.execute('begin immediate')
        try:
            with self.assertRaises((sqlite3.OperationalError,TimeoutError)):
                coordinated_backup(self.financial,self.queue,self.observer,self.dest,timeout=.05)
            self.assertFalse(self.dest.exists())
            with sqlite3.connect(self.financial,isolation_level=None,timeout=.05) as db:
                db.execute('begin immediate');db.rollback()
        finally:blocker.rollback();blocker.close()
        coordinated_backup(self.financial,self.queue,self.observer,self.dest)
        self.assertTrue(self.dest.exists())
