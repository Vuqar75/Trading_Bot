import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.cross_snapshot_guard import financial_history_unchanged
from trading_bot.runtime_store import RuntimeStore
from trading_bot.futures_store import FuturesStore


class CrossSnapshotGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);spot=self.root/'spot.db';futures=self.root/'futures.db'
        s=RuntimeStore(spot,{'mode':'paper','interval':'15m'});f=FuturesStore(futures)
        s.close();f.close();self.live=self.root/'financial.db';self.old=self.root/'before.db'
        consolidate_snapshots(spot,futures,self.live,captured_at=1)
        with closing(sqlite3.connect(self.live)) as src, closing(sqlite3.connect(self.old)) as dst:src.backup(dst)

    def test_identical_history_and_distinct_snapshots(self):
        self.assertTrue(financial_history_unchanged(self.old,self.live))
        with self.assertRaises(ValueError):financial_history_unchanged(self.live,self.live)

    def test_new_transfer_makes_snapshot_restore_unsafe(self):
        shared=SharedPaperStore(self.live);self.addCleanup(shared.close)
        p=shared.propose_transfer('transfer','spot','futures_isolated','0.1',now=100)
        shared.confirm_transfer(p['id'],now=100)
        self.assertFalse(financial_history_unchanged(self.old,self.live))

    def test_new_financial_event_makes_snapshot_restore_unsafe(self):
        shared=SharedPaperStore(self.live);self.addCleanup(shared.close)
        shared.apply('spot','fee-event',{'fixture':True},lambda db:shared._set('spot','cash','999.9'))
        self.assertFalse(financial_history_unchanged(self.old,self.live))

    def test_each_new_event_type_blocks_snapshot_rollback(self):
        for kind in ('CROSS_OPEN','CROSS_CLOSE','CROSS_FUNDING','TRANSFER','SPOT_FILL'):
            with self.subTest(kind=kind):
                path=self.root/(kind+'.db')
                with closing(sqlite3.connect(self.old)) as source, closing(sqlite3.connect(path)) as target:source.backup(target)
                with closing(SharedPaperStore(path)) as shared:
                    with shared.transaction():
                        shared.ledger._post('new:'+kind,kind,{'fixture':True},
                                            [('external:fixture_a','USDT','-0.01'),('external:fixture_b','USDT','0.01')])
                self.assertFalse(financial_history_unchanged(self.old,path))
