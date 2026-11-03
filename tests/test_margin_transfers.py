import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from trading_bot.futures_store import FuturesStore
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore


class MarginTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        spot=RuntimeStore(root/'spot.db',{'mode':'paper','interval':'15m'});future=FuturesStore(root/'future.db')
        future.command(1,True,'/resume');future.open_position('SHORT',Decimal(100),1000,'entry');future.command(2,True,'/pause')
        self.position=future.get('position');spot.close();future.close()
        consolidate_snapshots(root/'spot.db',root/'future.db',root/'shared.db',captured_at=1)
        self.shared=SharedPaperStore(root/'shared.db');self.addCleanup(self.shared.close)
        self.book=MarginPaperBook(self.shared.ledger)
        f={'lot_rules':[{'min':'.0001','max':'100','step':'.0001'}],'min_notional':'0','max_notional':'0','observed_ms':100000}
        self.quotes={'BTCUSDT':{'price':'100','time_ms':100000,'server_ms':100000,'source':'fixture','price_kind':'last_public_trade','filters':f},
                     'ETHUSDT':{'price':'200','time_ms':100000,'server_ms':100000,'source':'fixture','price_kind':'last_public_trade','filters':f}}
        self.book.record_quotes(self.quotes,now_ms=100000)

    def test_fund_margin_once_without_touching_isolated_position_or_equity(self):
        before=self.shared.snapshot('futures');cash=self.shared.ledger.balance('futures_isolated:free')
        p=self.shared.propose_transfer('initial-margin','futures_isolated','margin_cross','50',now=100)
        event=self.shared.confirm_transfer(p['id'],now=101)
        self.assertEqual(self.shared.confirm_transfer(p['id'],now=102),event)
        self.assertEqual(self.shared.ledger.balance('margin_cross:free'),Decimal(50))
        self.assertEqual(self.shared.ledger.balance('futures_isolated:free'),cash-50)
        self.assertEqual(self.shared.snapshot('futures')['position'],before['position'])
        self.assertEqual(self.shared.ledger.balance('futures_isolated:free')+self.shared.ledger.balance('margin_cross:free'),cash)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_outgoing_margin_transfer_requires_pause_fresh_prices_and_free_collateral(self):
        p=self.shared.propose_transfer('fund','futures_isolated','margin_cross','50',now=100);self.shared.confirm_transfer(p['id'],now=100)
        with self.shared.transaction():self.shared.db.execute("UPDATE wallet_state SET paused=0 WHERE wallet='margin_cross'")
        self.book.open('long','position','BTCUSDT','LONG','.09','5',2,self.quotes,now_ms=100000)
        with self.assertRaisesRegex(ValueError,'paused'):
            self.shared.propose_transfer('unpaused','margin_cross','reserve','1',now=100)
        with self.shared.transaction():self.shared.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='margin_cross'")
        with self.assertRaisesRegex(ValueError,'stale'):
            self.shared.propose_transfer('stale','margin_cross','reserve','1',now=300)
        with self.assertRaisesRegex(ValueError,'collateral'):
            self.shared.propose_transfer('too-much','margin_cross','reserve','49',now=100)
        allowed=self.shared.propose_transfer('safe','margin_cross','reserve','1',now=100)
        event=self.shared.confirm_transfer(allowed['id'],now=101)
        self.assertEqual(self.shared.confirm_transfer(allowed['id'],now=102),event)
        self.assertEqual(self.shared.ledger.balance('reserve:free'),1)
        self.assertEqual(len(self.book._positions()),1)
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])


if __name__=='__main__':unittest.main()
