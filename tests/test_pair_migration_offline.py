import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from tests.test_paper_pair_catalog import spot_info
from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.futures_store import FuturesStore
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_pair_runtime_offline import restore_margin_book
from trading_bot.paper_pair_selection_offline import (select_spot_pair_offline,select_isolated_pair_offline,
                                                      select_cross_pairs_offline,select_margin_pairs_offline)
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.paper_wallet_live_report import live_report
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue


class PairMigrationOfflineTests(unittest.TestCase):
    def test_all_four_selections_preserve_capital_at_fixed_prices_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);spot=root/'spot.db';future=root/'future.db';financial=root/'financial.db';queue=root/'queue.db'
            s=RuntimeStore(spot,{'mode':'paper','symbol':'BTCUSDT'});s.close()
            f=FuturesStore(future);f.close();consolidate_snapshots(spot,future,financial,captured_at=1)
            q=UnifiedQueue(queue);q.close();shared=SharedPaperStore(financial)
            try:
                CrossPaperBook(shared);margin=MarginPaperBook(shared.ledger)
                with shared.transaction():
                    shared.ledger._post('fixture:old-eth','FIXTURE',{},[
                        ('margin_cross:free','ETH',Decimal('.001')),('external:fixture','ETH',Decimal('-.001'))])
                def quotes(book):
                    rules={'lot_rules':[{'min':'0','max':'0','step':'.001'}],
                           'min_notional':'0','max_notional':'0','observed_ms':100000}
                    return {symbol:{'price':'100','time_ms':100000,'server_ms':100000,'source':'fixture',
                                    'price_kind':'last_public_trade','filters':rules} for symbol in book.pairs}
                margin.record_quotes(quotes(margin),now_ms=100000)
                before=live_report(shared,now=100)
                self.assertIsNotNone(before['total'])
                history=[tuple(r) for r in shared.db.execute('SELECT * FROM ledger_postings ORDER BY event_id,ordinal')]
                version=dict(expected_symbol='BTCUSDT',expected_version=0,symbol='SOLUSDT',price='100',quantity='.1',observed_at=100,clock=lambda:101)
                select_spot_pair_offline(shared,queue,request_id='spot:migrate',metadata=spot_info(),**version)
                finfo=spot_info();finfo.update(contractType='PERPETUAL',marginAsset='USDT')
                select_isolated_pair_offline(shared,queue,request_id='future:migrate',metadata=finfo,**version)
                spotrows={'BTCUSDT':spot_info('BTC'),'SOLUSDT':spot_info('SOL')}
                futrows={s:dict(row,contractType='PERPETUAL',marginAsset='USDT') for s,row in spotrows.items()}
                common=dict(expected_symbols=['BTCUSDT','ETHUSDT'],expected_version=0,symbols=['BTCUSDT','SOLUSDT'],
                            prices={'BTCUSDT':'100','SOLUSDT':'100'},quantities={'BTCUSDT':'.1','SOLUSDT':'.1'},
                            observed_at=100,clock=lambda:101)
                select_cross_pairs_offline(shared,queue,request_id='cross:migrate',metadata=futrows,**common)
                select_margin_pairs_offline(shared,queue,request_id='margin:migrate',metadata=spotrows,**common)
                margin=restore_margin_book(shared);margin.record_quotes(quotes(margin),now_ms=100000)
                after=live_report(shared,now=100)
                self.assertEqual(before['total'],after['total'])
                self.assertEqual(history,[tuple(r) for r in shared.db.execute('SELECT * FROM ledger_postings ORDER BY event_id,ordinal')])
                self.assertEqual(shared.ledger.balance('margin_cross:free','ETH'),Decimal('.001'))
                self.assertEqual(shared.ledger.balance('margin_cross:free','SOL'),0)
                self.assertTrue(shared.snapshot('spot')['paused']);self.assertTrue(shared.snapshot('futures')['paused'])
                self.assertTrue(shared.ledger.reconcile()['balanced'])
            finally:shared.close()
