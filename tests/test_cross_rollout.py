import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.cross_rollout import allocate_initial_cross
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue


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

    def test_allocation_is_atomic_exact_and_idempotent(self):
        result=self.allocate();self.assertFalse(result['replayed'])
        self.assertEqual(self.shared.ledger.balance('futures_isolated:free'),Decimal(940))
        self.assertEqual(self.shared.ledger.balance('futures_cross:free'),Decimal(50))
        self.assertEqual(self.shared.snapshot('futures')['position'],self.existing_position)
        self.assertEqual(self.shared.ledger.balance('futures_isolated:margin'),Decimal(10))
        self.assertFalse(self.shared.deployment_hold())
        self.assertTrue(self.allocate()['replayed'])
        self.assertEqual(self.shared.db.execute("SELECT count(*) FROM ledger_events WHERE kind='TRANSFER'").fetchone()[0],1)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

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
