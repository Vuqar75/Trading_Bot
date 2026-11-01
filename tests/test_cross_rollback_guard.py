import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.cross_rollback_guard import check_cross_code_rollback
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue


class CrossRollbackGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);spot=root/'spot.db';future=root/'future.db';self.fin=root/'shared.db';self.queue=root/'queue.db';self.base=root/'baseline.db'
        a=RuntimeStore(spot,{'mode':'paper','interval':'15m'});b=FuturesStore(future);a.close();b.close()
        consolidate_snapshots(spot,future,self.fin,captured_at=1)
        self.shared=SharedPaperStore(self.fin);self.addCleanup(self.shared.close)
        self.book=CrossPaperBook(self.shared)
        self.q=UnifiedQueue(self.queue);self.addCleanup(self.q.close)
        with closing(sqlite3.connect(self.base)) as out:self.q.db.backup(out)

    def check(self):return check_cross_code_rollback(self.fin,self.queue,self.base)

    def test_old_code_can_reuse_current_database_when_cross_has_no_money(self):
        self.assertTrue(self.check()['safe_for_old_code'])
        def debit(db):self.shared._set('spot','cash','999');return {'ok':True}
        self.shared.apply('spot','legitimate-later',{'test':True},debit)
        self.assertTrue(self.check()['safe_for_old_code'])
        self.assertEqual(self.shared.ledger.balance('spot:free'),Decimal(999))

    def test_transfer_to_cross_refuses_old_code_even_if_balance_later_zero(self):
        proposal=self.shared.propose_transfer('cross-allocation','futures_isolated','futures_cross','50',now=100)
        self.shared.confirm_transfer(proposal['id'],now=100)
        result=self.check();self.assertFalse(result['safe_for_old_code'])
        self.assertIn('cross_financial_history',result['reasons'])

    def test_new_mode_or_pending_command_refuses_rollback(self):
        self.q.receive(1,True,'/mode futures_cross')
        result=self.check();self.assertFalse(result['safe_for_old_code'])
        self.assertIn('new_cross_command',result['reasons'])
        self.q.receive(2,True,'/pause')
        self.assertIn('pending_commands',self.check()['reasons'])


if __name__=='__main__':unittest.main()
