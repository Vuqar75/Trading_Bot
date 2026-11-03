import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.futures_store import FuturesStore
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.report_update_guard import freeze_report_update,release_report_update
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue


class ReportUpdateGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);p=Path(self.tmp.name)
        spot=RuntimeStore(p/'spot.db',{'mode':'paper','interval':'15m'});future=FuturesStore(p/'future.db')
        future.command(1,True,'/resume');future.open_position('SHORT',Decimal(100),1000,'old');self.old_position=future.get('position')
        future.command(2,True,'/pause');spot.close();future.close();consolidate_snapshots(p/'spot.db',p/'future.db',p/'shared.db',captured_at=1)
        self.shared=SharedPaperStore(p/'shared.db');self.q=UnifiedQueue(p/'queue.db')
        self.addCleanup(self.shared.close);self.addCleanup(self.q.close)
        with self.shared.transaction():
            self.shared._set('spot','quote',{'price':'100','time':100});self.shared._set('futures','quote',{'price':'100','time':100,'server_ms':1000})
            self.shared._set('spot','paused',False);self.shared._set('futures','paused',True)
        cross=CrossPaperBook(self.shared)
        cq={s:{'mark':'100','time':100,'funding_complete':True,'server_ms':100000,'next_due_ms':28000000,'interval_ms':28800000} for s in ('BTCUSDT','ETHUSDT')}
        cross.record_quotes(cq,now=100)
        margin=MarginPaperBook(self.shared.ledger)
        f={'lot_rules':[{'min':'0','max':'0','step':'.0001'}],'min_notional':'0','max_notional':'0','observed_ms':100000}
        quotes={s:{'price':'100','time_ms':100000,'server_ms':100000,'source':'fixture','price_kind':'last_public_trade','filters':f} for s in ('BTCUSDT','ETHUSDT')}
        margin.record_quotes(quotes,now_ms=100000)

    def freeze(self):return freeze_report_update(self.shared,self.q,'reports:freeze:test',now=100)
    def release(self,checked=100000):
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):return release_report_update(self.shared,self.q,'reports:release:test',protection_checked_ms=checked,now=100)

    def test_original_pauses_and_financial_history_preserved(self):
        before=self.shared.db.execute('SELECT id,fingerprint FROM ledger_events ORDER BY id').fetchall()
        frozen=self.freeze();self.assertEqual(frozen['paused'],{'spot':False,'futures':True,'futures_cross':True,'margin_cross':True})
        self.assertTrue(self.freeze()['replayed'])
        self.assertEqual(self.release()['enabled'],['spot'])
        self.assertEqual(self.shared.snapshot('futures')['position'],self.old_position)
        self.assertEqual([tuple(r) for r in before],[tuple(r) for r in self.shared.db.execute('SELECT id,fingerprint FROM ledger_events ORDER BY id')])
        with self.shared.transaction():self.shared._set('spot','paused',True)
        self.assertTrue(self.release()['replayed']);self.assertTrue(self.shared.snapshot('spot')['paused'])

    def test_manual_pause_after_freeze_and_expired_protection(self):
        self.freeze()
        with self.assertRaisesRegex(ValueError,'protection stale'):self.release(checked=-30000)
        self.q.receive(1,True,'/pause_all',now=100)
        for market in ('spot','futures','futures_cross','margin_cross'):self.q.acknowledge(self.q.pending(market)[0]['id'],'paused')
        self.assertEqual(self.release()['enabled'],[]);self.assertTrue(self.shared.snapshot('spot')['paused'])

    def test_before_and_after_commit_failure_are_safe(self):
        def before(where):
            if where=='before_commit':raise RuntimeError('before')
        self.shared.fault=before
        try:
            with self.assertRaises(RuntimeError):self.freeze()
        finally:self.shared.fault=lambda p:None
        self.assertFalse(self.shared.deployment_hold());self.assertFalse(self.shared.snapshot('spot')['paused'])
        self.freeze()
        def after(where):
            if where=='after_commit':raise RuntimeError('after')
        self.shared.fault=after
        try:
            with self.assertRaises(RuntimeError):self.release()
        finally:self.shared.fault=lambda p:None
        with self.shared.transaction():self.shared._set('spot','paused',True)
        self.assertTrue(self.release()['replayed']);self.assertTrue(self.shared.snapshot('spot')['paused'])

    def test_new_namespace_preserves_prior_checkpoint_and_pauses(self):
        self.freeze();self.release()
        prior=[tuple(row) for row in self.shared.db.execute("SELECT key,value FROM ledger_meta WHERE key LIKE 'reports_update_%' ORDER BY key")]
        frozen=freeze_report_update(self.shared,self.q,'diagnostics:freeze:test',now=100,namespace='diagnostics')
        self.assertFalse(frozen['paused']['spot']);self.assertTrue(frozen['paused']['margin_cross'])
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):
            done=release_report_update(self.shared,self.q,'diagnostics:release:test',protection_checked_ms=100000,now=100,namespace='diagnostics')
        self.assertEqual(done['enabled'],['spot'])
        self.assertEqual(prior,[tuple(row) for row in self.shared.db.execute("SELECT key,value FROM ledger_meta WHERE key LIKE 'reports_update_%' ORDER BY key")])

    def test_diagnostics_refuses_unpaused_margin_without_changing_any_pause(self):
        with self.shared.transaction():self.shared.db.execute("UPDATE wallet_state SET paused=0 WHERE wallet='margin_cross'")
        with self.assertRaisesRegex(ValueError,'margin and cross paused'):
            freeze_report_update(self.shared,self.q,'diagnostics:freeze:test',now=100,namespace='diagnostics')
        self.assertFalse(self.shared.deployment_hold());self.assertFalse(self.shared.snapshot('spot')['paused'])
        self.assertEqual(self.shared.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0],0)


if __name__=='__main__':unittest.main()
