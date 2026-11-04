import time
"""Third-asset evidence must not be silently omitted from Spot accounting."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from trading_bot.futures_store import FuturesStore
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.paper_pair_selection_offline import select_spot_pair_offline
from trading_bot.unified_queue import UnifiedQueue
from trading_bot.paper_wallet_live_report import live_report, render_live_report
from tests.test_paper_pair_catalog import spot_info


class PairSharedGuardTests(unittest.TestCase):
    def test_untracked_third_asset_fails_spot_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            spot=RuntimeStore(root/'spot.db',{'mode':'paper','symbol':'BTCUSDT'})
            futures=FuturesStore(root/'futures.db')
            spot.close();futures.close()
            consolidate_snapshots(root/'spot.db',root/'futures.db',root/'shared.db',captured_at=1)
            shared=SharedPaperStore(root/'shared.db')
            try:
                for account in ('spot:position','spot:reserved'):
                    with self.subTest(account=account):
                        with shared.ledger.transaction():
                            shared.ledger._post('fixture:unexpected-sol:'+account,'FIXTURE',{},[
                                (account,'SOL','1.25'),('external:fixture','SOL','-1.25')])
                        self.assertTrue(shared.ledger.reconcile()['balanced'])
                        with self.assertRaisesRegex(ValueError,'untracked Spot asset'):
                            shared._assert_consistent('spot')
                        with shared.ledger.transaction():
                            shared.ledger._post('fixture:restore-sol:'+account,'FIXTURE',{},[
                                (account,'SOL','-1.25'),('external:fixture','SOL','1.25')])
                        shared._assert_consistent('spot')
            finally:shared.close()

    def test_sol_paper_fills_and_fees_reconcile_on_isolated_shared_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            spot=RuntimeStore(root/'spot.db',{'mode':'paper','symbol':'BTCUSDT'})
            futures=FuturesStore(root/'futures.db');spot.close();futures.close()
            consolidate_snapshots(root/'spot.db',root/'futures.db',root/'shared.db',captured_at=1)
            shared=SharedPaperStore(root/'shared.db')
            try:
                queue=UnifiedQueue(root/'queue.db');queue.close()
                select_spot_pair_offline(shared,root/'queue.db',request_id='fixture:choose-sol',
                    expected_symbol='BTCUSDT',expected_version=0,symbol='SOLUSDT',
                    metadata=spot_info(),price='100',quantity='.1',observed_at=100,clock=lambda:101)
            finally:shared.close()
            store=RuntimeStore(root/'shared.db',{'mode':'paper','symbol':'SOLUSDT'},shared=True)
            try:
                with store.transaction():store.set('paused',False)
                client=store.prepare('BUY',Decimal('.1'),Decimal('100'),'sol-buy','fixture',quote_at=time.time())
                store.claim(client)
                row=store.db.execute('SELECT * FROM runtime_orders WHERE client_id=?',(client,)).fetchone()
                report=PaperExchange.inspect_saved({**dict(row),'symbol':'SOLUSDT'})
                report['fills'][0]['commissionAsset']='SOL';report['fills'][0]['commission']='0.001'
                store.apply(client,report);store.apply(client,report)
                pos=store.get('position')
                self.assertEqual(pos['symbol'],'SOLUSDT')
                self.assertEqual(store.shared.ledger.balance('spot:position','SOL'),Decimal('.099'))
                self.assertEqual(store.shared.ledger.balance('spot:position','BTC'),Decimal(0))
                with store.transaction():
                    store.set('quote',{'symbol':'SOLUSDT','price':'110','time':100})
                valuation=live_report(store.shared,now=101)
                self.assertEqual(valuation['total'],Decimal('2000.89'))
                self.assertEqual(valuation['spot_open'],Decimal('.89'))
                self.assertEqual(valuation['spot_asset'],'SOL')
                self.assertIn('SOL',render_live_report(valuation))
                with store.transaction():
                    store.set('quote',{'symbol':'BTCUSDT','price':'110','time':100})
                incomplete=live_report(store.shared,now=101)
                self.assertIsNone(incomplete['total'])
                self.assertIsNone(incomplete['spot_open'])
                sell=store.prepare('SELL',Decimal('.099'),Decimal('110'),'sol-sell','fixture',target=pos['id'],quote_at=time.time())
                store.claim(sell)
                row=store.db.execute('SELECT * FROM runtime_orders WHERE client_id=?',(sell,)).fetchone()
                closing=PaperExchange.inspect_saved({**dict(row),'symbol':'SOLUSDT'})
                store.apply(sell,closing);store.apply(sell,closing)
                self.assertEqual(Decimal(store.get('pnl')),Decimal('.87911'))
                self.assertEqual(store.shared.ledger.balance('spot:position','SOL'),Decimal(0))
                self.assertTrue(store.shared.ledger.reconcile()['balanced'])
            finally:store.close()
            store=RuntimeStore(root/'shared.db',{'mode':'paper','symbol':'SOLUSDT'},shared=True)
            try:self.assertEqual(Decimal(store.get('pnl')),Decimal('.87911'))
            finally:store.close()
