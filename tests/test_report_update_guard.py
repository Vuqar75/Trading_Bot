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

    def test_boundary_refuses_unpaused_isolated_without_changes(self):
        with self.shared.transaction():self.shared._set('futures','paused',False)
        with self.assertRaisesRegex(ValueError,'isolated futures paused'):
            freeze_report_update(self.shared,self.q,'boundary:freeze:test',now=100,namespace='boundary')
        self.assertFalse(self.shared.deployment_hold())
        self.assertFalse(self.shared.snapshot('spot')['paused'])
        self.assertFalse(self.shared.snapshot('futures')['paused'])

    def test_boundary_rollout_preserves_all_prior_checkpoints_and_pauses(self):
        for namespace in ('reports','diagnostics','resilience'):
            for state in ('freeze','release'):
                self.shared.db.execute('INSERT INTO ledger_meta VALUES(?,?)',(namespace+'_update_'+state,'{}'))
        before=[tuple(r) for r in self.shared.db.execute("SELECT key,value FROM ledger_meta WHERE key LIKE '%update_%' ORDER BY key")]
        frozen=freeze_report_update(self.shared,self.q,'boundary:freeze:test',now=100,namespace='boundary')
        self.assertEqual(frozen['paused'],{'spot':False,'futures':True,'futures_cross':True,'margin_cross':True})
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):
            result=release_report_update(self.shared,self.q,'boundary:release:test',protection_checked_ms=100000,now=100,namespace='boundary')
        self.assertEqual(result['enabled'],['spot'])
        after=[tuple(r) for r in self.shared.db.execute("SELECT key,value FROM ledger_meta WHERE key LIKE '%update_%' AND key NOT LIKE 'boundary_%' ORDER BY key")]
        self.assertEqual(before,after)
        self.assertEqual(self.shared.snapshot('futures')['position'],self.old_position)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_finalaudit_keeps_prior_checkpoints_and_isolated_paused(self):
        for namespace in ('reports','diagnostics','resilience','boundary'):
            for state in ('freeze','release'):
                self.shared.db.execute('INSERT INTO ledger_meta VALUES(?,?)',(namespace+'_update_'+state,'{}'))
        before=[tuple(r) for r in self.shared.db.execute("SELECT key,value FROM ledger_meta WHERE key LIKE '%update_%' ORDER BY key")]
        frozen=freeze_report_update(self.shared,self.q,'finalaudit:freeze:test',now=100,namespace='finalaudit')
        self.assertEqual(frozen['paused'],{'spot':False,'futures':True,'futures_cross':True,'margin_cross':True})
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):
            result=release_report_update(self.shared,self.q,'finalaudit:release:test',protection_checked_ms=100000,now=100,namespace='finalaudit')
        self.assertEqual(result['enabled'],['spot'])
        self.assertEqual(before,[tuple(r) for r in self.shared.db.execute("SELECT key,value FROM ledger_meta WHERE key LIKE '%update_%' AND key NOT LIKE 'finalaudit_update_%' ORDER BY key")])

    def test_finalaudit_refuses_unpaused_isolated(self):
        with self.shared.transaction():self.shared._set('futures','paused',False)
        with self.assertRaisesRegex(ValueError,'isolated futures paused'):
            freeze_report_update(self.shared,self.q,'finalaudit:freeze:test',now=100,namespace='finalaudit')
        self.assertFalse(self.shared.deployment_hold())

    def test_adaptive_shadow_restores_only_previously_enabled_spot(self):
        frozen=freeze_report_update(self.shared,self.q,'adaptiveshadow:freeze:test',now=100,namespace='adaptiveshadow')
        self.assertFalse(frozen['paused']['spot']);self.assertTrue(frozen['paused']['futures'])
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):
            result=release_report_update(self.shared,self.q,'adaptiveshadow:release:test',protection_checked_ms=100000,now=100,namespace='adaptiveshadow')
        self.assertEqual(result['enabled'],['spot']);self.assertFalse(self.shared.snapshot('spot')['paused']);self.assertTrue(self.shared.snapshot('futures')['paused'])

    def test_adaptive_ai_has_its_own_hold_and_restores_prior_spot(self):
        frozen=freeze_report_update(self.shared,self.q,'adaptiveai:freeze:test',now=100,namespace='adaptiveai')
        self.assertFalse(frozen['paused']['spot']);self.assertTrue(frozen['paused']['futures'])
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):
            result=release_report_update(self.shared,self.q,'adaptiveai:release:test',protection_checked_ms=100000,now=100,namespace='adaptiveai')
        self.assertEqual(result['enabled'],['spot']);self.assertFalse(self.shared.snapshot('spot')['paused']);self.assertTrue(self.shared.snapshot('futures')['paused'])

    def test_adaptive_diagnostic_has_its_own_hold(self):
        frozen=freeze_report_update(self.shared,self.q,'adaptivediag:freeze:test',now=100,namespace='adaptivediag')
        self.assertFalse(frozen['paused']['spot'])
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):
            result=release_report_update(self.shared,self.q,'adaptivediag:release:test',protection_checked_ms=100000,now=100,namespace='adaptivediag')
        self.assertEqual(result['enabled'],['spot']);self.assertTrue(self.shared.snapshot('futures')['paused'])

    def test_regular_ai_has_its_own_hold(self):
        frozen=freeze_report_update(self.shared,self.q,'regularai:freeze:test',now=100,namespace='regularai')
        self.assertFalse(frozen['paused']['spot'])
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):
            result=release_report_update(self.shared,self.q,'regularai:release:test',protection_checked_ms=100000,now=100,namespace='regularai')
        self.assertEqual(result['enabled'],['spot']);self.assertTrue(self.shared.snapshot('futures')['paused'])

    def test_ai_view_does_not_reuse_previous_regular_ai_hold(self):
        freeze_report_update(self.shared,self.q,'regularai:freeze:test',now=100,namespace='regularai')
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):
            release_report_update(self.shared,self.q,'regularai:release:test',protection_checked_ms=100000,now=100,namespace='regularai')
        old=self.shared.db.execute("SELECT value FROM ledger_meta WHERE key='regularai_update_freeze'").fetchone()[0]
        frozen=freeze_report_update(self.shared,self.q,'aiview:freeze:test',now=100,namespace='aiview')
        self.assertFalse(frozen['paused']['spot'])
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):
            result=release_report_update(self.shared,self.q,'aiview:release:test',protection_checked_ms=100000,now=100,namespace='aiview')
        self.assertEqual(result['enabled'],['spot'])
        self.assertEqual(old,self.shared.db.execute("SELECT value FROM ledger_meta WHERE key='regularai_update_freeze'").fetchone()[0])

    def test_pair_namespace_requires_and_preserves_all_pauses(self):
        with self.assertRaisesRegex(ValueError,'Spot must remain paused'):
            freeze_report_update(self.shared,self.q,'pair:freeze:test',now=100,namespace='pair')
        self.assertFalse(self.shared.deployment_hold())
        with self.shared.transaction():self.shared._set('spot','paused',True)
        frozen=freeze_report_update(self.shared,self.q,'pair:freeze:test',now=100,namespace='pair')
        self.assertTrue(all(frozen['paused'].values()))
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):
            result=release_report_update(self.shared,self.q,'pair:release:test',protection_checked_ms=100000,now=100,namespace='pair')
        self.assertEqual(result['enabled'],[])
        self.assertTrue(self.shared.snapshot('spot')['paused']);self.assertTrue(self.shared.snapshot('futures')['paused'])

    def test_inline_namespace_requires_and_preserves_all_pauses(self):
        with self.assertRaisesRegex(ValueError,'Spot must remain paused'):
            freeze_report_update(self.shared,self.q,'inline:freeze:test',now=100,namespace='inline')
        with self.shared.transaction():self.shared._set('spot','paused',True)
        frozen=freeze_report_update(self.shared,self.q,'inline:freeze:test',now=100,namespace='inline')
        self.assertTrue(all(frozen['paused'].values()))
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):
            result=release_report_update(self.shared,self.q,'inline:release:test',protection_checked_ms=100000,now=100,namespace='inline')
        self.assertEqual(result['enabled'],[])
        self.assertTrue(self.shared.snapshot('spot')['paused']);self.assertTrue(self.shared.snapshot('futures')['paused'])

    def test_pairfix_namespace_requires_and_preserves_all_pauses(self):
        with self.assertRaisesRegex(ValueError,'Spot must remain paused'):
            freeze_report_update(self.shared,self.q,'pairfix:freeze:test',now=100,namespace='pairfix')
        with self.shared.transaction():self.shared._set('spot','paused',True)
        frozen=freeze_report_update(self.shared,self.q,'pairfix:freeze:test',now=100,namespace='pairfix')
        self.assertTrue(all(frozen['paused'].values()))
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):
            result=release_report_update(self.shared,self.q,'pairfix:release:test',protection_checked_ms=100000,now=100,namespace='pairfix')
        self.assertEqual(result['enabled'],[])

    def test_healthdiag_namespace_requires_and_preserves_all_pauses(self):
        with self.assertRaisesRegex(ValueError,'Spot must remain paused'):
            freeze_report_update(self.shared,self.q,'healthdiag:freeze:test',now=100,namespace='healthdiag')
        with self.shared.transaction():self.shared._set('spot','paused',True)
        frozen=freeze_report_update(self.shared,self.q,'healthdiag:freeze:test',now=100,namespace='healthdiag')
        self.assertTrue(all(frozen['paused'].values()))
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):
            result=release_report_update(self.shared,self.q,'healthdiag:release:test',protection_checked_ms=100000,now=100,namespace='healthdiag')
        self.assertEqual(result['enabled'],[])

    def test_resilience_namespace_keeps_earlier_deployment_records(self):
        self.shared.db.execute("INSERT INTO ledger_meta VALUES('diagnostics_update_freeze','{}')")
        before=self.shared.db.execute("SELECT value FROM ledger_meta WHERE key='diagnostics_update_freeze'").fetchone()[0]
        freeze_report_update(self.shared,self.q,'resilience:freeze:test',now=100,namespace='resilience')
        with patch('trading_bot.paper_shared_store.time.time',return_value=100):
            released=release_report_update(self.shared,self.q,'resilience:release:test',protection_checked_ms=100000,now=100,namespace='resilience')
        self.assertEqual(released['enabled'],['spot'])
        self.assertEqual(self.shared.db.execute("SELECT value FROM ledger_meta WHERE key='diagnostics_update_freeze'").fetchone()[0],before)


if __name__=='__main__':unittest.main()
