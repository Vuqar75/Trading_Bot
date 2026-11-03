import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.futures_store import FuturesStore
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.margin_rollout import freeze_margin_cutover,allocate_margin_once,release_margin_cutover
from trading_bot.margin_valuation_verifier import verify_allocation_value
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue


class MarginRolloutTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);p=Path(self.tmp.name)
        spot=RuntimeStore(p/'spot.db',{'mode':'paper','interval':'15m'});future=FuturesStore(p/'future.db')
        future.command(1,True,'/resume');future.open_position('SHORT',Decimal(100),1000,'old')
        self.old_position=future.get('position');future.command(2,True,'/pause');spot.close();future.close()
        consolidate_snapshots(p/'spot.db',p/'future.db',p/'shared.db',captured_at=1)
        self.shared=SharedPaperStore(p/'shared.db');self.addCleanup(self.shared.close)
        with self.shared.transaction():
            self.shared._set('spot','quote',{'price':'100','time':100});self.shared._set('futures','quote',{'price':'100','time':100,'server_ms':1000})
            self.shared._set('spot','paused',False);self.shared._set('futures','paused',True)
        cross=CrossPaperBook(self.shared)
        quotes={s:{'mark':'100','time':100,'funding_complete':True,'server_ms':100000,'next_due_ms':28000000,'interval_ms':28800000} for s in ('BTCUSDT','ETHUSDT')}
        cross.record_quotes(quotes,now=100)
        self.q=UnifiedQueue(p/'queue.db');self.addCleanup(self.q.close)

    def freeze(self):return freeze_margin_cutover(self.shared,self.q,'margin:freeze:test',now=100)
    def initialize(self):
        book=MarginPaperBook(self.shared.ledger)
        f={'lot_rules':[{'min':'0','max':'0','step':'.0001'}],'min_notional':'0','max_notional':'0','observed_ms':100000}
        quotes={s:{'price':'100','time_ms':100000,'server_ms':100000,'source':'fixture','price_kind':'last_public_trade','filters':f} for s in ('BTCUSDT','ETHUSDT')}
        book.record_quotes(quotes,now_ms=100000);return book
    def allocate(self):
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):return allocate_margin_once(self.shared,self.q,'margin:initial:test',now=100)
    def release(self,protection=100000):
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):return release_margin_cutover(self.shared,self.q,'margin:release:test',now=100,protection_checked_ms=protection)

    def test_single_allocation_and_original_pauses(self):
        self.assertEqual(self.freeze()['paused'],{'spot':False,'futures':True});self.assertTrue(self.freeze()['replayed'])
        self.initialize();before=self.shared.ledger.balance('futures_isolated:free')
        first=self.allocate();self.assertEqual(first['event'],self.allocate()['event']);self.assertTrue(self.allocate()['replayed'])
        self.assertEqual(self.shared.ledger.balance('futures_isolated:free'),before-50)
        self.assertEqual(self.shared.ledger.balance('margin_cross:free'),Decimal(50))
        self.assertEqual(self.shared.snapshot('futures')['position'],self.old_position)
        result=self.release();self.assertEqual(result['enabled'],['spot','margin_cross'])
        self.assertFalse(self.shared.snapshot('spot')['paused']);self.assertTrue(self.shared.snapshot('futures')['paused'])
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='futures_cross'").fetchone()[0],1)
        with self.shared.transaction():self.shared._set('spot','paused',True);self.shared.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='margin_cross'")
        self.assertTrue(self.release()['replayed']);self.assertTrue(self.shared.snapshot('spot')['paused'])
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_pause_all_after_freeze_is_not_overridden(self):
        self.freeze();self.initialize();self.allocate();self.q.receive(1,True,'/pause_all',now=100)
        for market in ('spot','futures','futures_cross','margin_cross'):
            self.q.acknowledge(self.q.pending(market)[0]['id'],'paused')
        self.assertEqual(self.release()['enabled'],[])
        self.assertTrue(self.shared.snapshot('spot')['paused'])
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0],1)

    def test_stale_protection_and_pending_commands_keep_hold(self):
        self.freeze();self.initialize();self.allocate()
        with self.assertRaisesRegex(ValueError,'protection'):self.release(protection=-30000)
        self.q.receive(1,True,'/pause',now=100)
        with self.assertRaisesRegex(ValueError,'Pending'):self.release()
        self.assertTrue(self.shared.deployment_hold());self.assertTrue(self.shared.snapshot('spot')['paused'])

    def test_allocation_replays_after_commit_fault_without_second_transfer(self):
        self.freeze();self.initialize()
        def crash(where):
            if where=='after_commit':raise RuntimeError('after commit')
        self.shared.fault=crash
        try:
            with self.assertRaises(RuntimeError):self.allocate()
        finally:self.shared.fault=lambda point:None
        self.assertTrue(self.allocate()['replayed'])
        self.assertEqual(self.shared.db.execute("SELECT count(*) FROM ledger_events WHERE kind='TRANSFER'").fetchone()[0],1)
        self.assertTrue(self.shared.deployment_hold())

    def test_freeze_and_allocation_before_commit_roll_back(self):
        def crash(where):
            if where=='before_commit':raise RuntimeError('before commit')
        self.shared.fault=crash
        try:
            with self.assertRaises(RuntimeError):self.freeze()
        finally:self.shared.fault=lambda point:None
        self.assertFalse(self.shared.deployment_hold());self.assertFalse(self.shared.snapshot('spot')['paused'])
        self.freeze();self.initialize();self.shared.fault=crash
        try:
            with self.assertRaises(RuntimeError):self.allocate()
        finally:self.shared.fault=lambda point:None
        self.assertEqual(self.shared.ledger.balance('margin_cross:free'),0)
        self.assertEqual(self.shared.db.execute("SELECT count(*) FROM ledger_events WHERE kind='TRANSFER'").fetchone()[0],0)
        self.assertTrue(self.shared.deployment_hold());self.allocate()

    def test_release_after_commit_fault_does_not_override_later_pause(self):
        self.freeze();self.initialize();self.allocate()
        def crash(where):
            if where=='after_commit':raise RuntimeError('after commit')
        self.shared.fault=crash
        try:
            with self.assertRaises(RuntimeError):self.release()
        finally:self.shared.fault=lambda point:None
        with self.shared.transaction():
            self.shared._set('spot','paused',True)
            self.shared.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='margin_cross'")
        self.assertTrue(self.release()['replayed'])
        self.assertTrue(self.shared.snapshot('spot')['paused'])
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0],1)

    def test_fixed_price_comparison_and_unexpected_event_guard(self):
        self.freeze();self.initialize();root=Path(self.tmp.name);before=root/'before.db';after=root/'after.db'
        db=sqlite3.connect(before)
        try:self.shared.db.backup(db)
        finally:db.close()
        allocation=self.allocate();db=sqlite3.connect(after)
        try:self.shared.db.backup(db)
        finally:db.close()
        result=verify_allocation_value(before,after,allocation['token'],now=100)
        self.assertEqual(result['capital_before'],result['capital_after']);self.assertTrue(result['history_preserved'])
        with self.shared.ledger.transaction():self.shared.ledger._post('unrelated','FIXTURE',{},[('reserve:free','USDT',Decimal(1)),('external:fixture','USDT',Decimal(-1))])
        db=sqlite3.connect(after)
        try:self.shared.db.backup(db)
        finally:db.close()
        with self.assertRaisesRegex(ValueError,'Unexpected financial events'):verify_allocation_value(before,after,allocation['token'],now=100)


if __name__=='__main__':unittest.main()
