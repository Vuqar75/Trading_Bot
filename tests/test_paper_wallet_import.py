import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from trading_bot.app import Application
from trading_bot.futures_store import FuturesStore
from trading_bot.paper_wallet_import import import_paper_snapshots
from trading_bot.paper_wallet_ledger import PaperWalletLedger
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.runtime_store import RuntimeStore


class WalletImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        p=Path(self.tmp.name);self.spot_path=p/'spot-copy.db';self.future_path=p/'futures-copy.db'
        spot=RuntimeStore(p/'spot.db',{'mode':'paper','interval':'15m'})
        fut=FuturesStore(p/'futures.db')
        try:
            app=Application(spot,PaperExchange(),None);spot.command(1,True,'/resume')
            app.execute('BUY','.1',Decimal(100),'buy','TEST')
            fut.command(1,True,'/resume');fut.open_position('SHORT',Decimal(100),1000,'entry')
            pos=fut.get('position');fut.apply_funding(pos['id'],[{'time':1500,'rate':'.001','mark':'100'}],2000)
            self.spot_expected=spot.get('position');self.future_expected=fut.get('position')
            self.spot_cash=spot.get('cash');self.futures_cash=fut.get('cash')
            with spot.transaction():spot.set('paused',True)
            with fut.transaction():fut.set('paused',True)
            for src,dest in ((spot.db,self.spot_path),(fut.db,self.future_path)):
                with sqlite3.connect(dest) as w:src.backup(w)
        finally:spot.close();fut.close()
        self.ledger=PaperWalletLedger(p/'new-ledger.db');self.addCleanup(self.ledger.close)

    def test_import_real_schema_snapshots_preserves_cash_position_and_funding(self):
        info=import_paper_snapshots(self.ledger,self.spot_path,self.future_path,captured_at=1234)
        self.assertTrue(info['spot_fees_known'])
        self.assertEqual(self.ledger.balance('spot:free'),Decimal(self.spot_cash))
        self.assertEqual(self.ledger.balance('spot:position','BTC'),Decimal(self.spot_expected['quantity']))
        self.assertEqual(self.ledger.balance('futures_isolated:free'),Decimal(self.futures_cash))
        self.assertEqual(self.ledger.balance('futures_isolated:margin'),Decimal(self.future_expected['margin']))
        self.assertEqual(self.ledger.balance('futures_isolated:funding'),Decimal(self.future_expected['funding']))
        self.assertEqual(self.ledger.balance('futures_isolated:entry_fee'),-Decimal(self.future_expected['entry_fee']))
        self.assertTrue(self.ledger.reconcile()['balanced'])
        with self.assertRaises(ValueError):import_paper_snapshots(self.ledger,self.spot_path,self.future_path,captured_at=1234)

    def test_pending_or_wrong_mode_blocks_import(self):
        with sqlite3.connect(self.spot_path) as db:
            db.execute("insert into runtime_orders values('cid','key','BUY','1','1','UNKNOWN',NULL,'TEST',0)")
        with self.assertRaises(ValueError):import_paper_snapshots(self.ledger,self.spot_path,self.future_path,captured_at=1234)
        self.assertEqual(self.ledger.balance('spot:free'),0)

    def test_wal_sidecar_or_same_file_is_rejected(self):
        Path(str(self.spot_path)+'-wal').touch()
        with self.assertRaises(ValueError):import_paper_snapshots(self.ledger,self.spot_path,self.future_path,captured_at=1234)
        with self.assertRaises(ValueError):import_paper_snapshots(self.ledger,self.future_path,self.future_path,captured_at=1234)

    def test_unpaused_snapshot_is_rejected(self):
        with sqlite3.connect(self.future_path) as db:
            db.execute("update futures_meta set value='false' where key='paused'")
        with self.assertRaises(ValueError):import_paper_snapshots(self.ledger,self.spot_path,self.future_path,captured_at=1234)
        self.assertEqual(self.ledger.balance('spot:free'),0)
