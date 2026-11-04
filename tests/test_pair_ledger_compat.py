"""Offline compatibility probes; they do not enable pair selection."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from trading_bot.paper_wallet_ledger import PaperWalletLedger


class PairLedgerCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'paper.db'

    def test_third_asset_postings_preserve_btc_history_and_replay(self):
        ledger = PaperWalletLedger(self.path)
        try:
            original = [('spot:position', 'BTC', '0.125'), ('external:fixture', 'BTC', '-0.125')]
            new = [('spot:position', 'SOL', '1.234'), ('external:fixture', 'SOL', '-1.234')]
            with ledger.transaction():
                ledger._post('btc-history', 'FIXTURE', {'symbol':'BTCUSDT'}, original)
                before = tuple(ledger.db.execute("SELECT fingerprint,details FROM ledger_events WHERE id='btc-history'").fetchone())
                self.assertTrue(ledger._post('sol-event', 'FIXTURE', {'symbol':'SOLUSDT'}, new))
                self.assertFalse(ledger._post('sol-event', 'FIXTURE', {'symbol':'SOLUSDT'}, new))
            self.assertEqual(ledger.balance('spot:position','BTC'), Decimal('0.125'))
            self.assertEqual(ledger.balance('spot:position','SOL'), Decimal('1.234'))
            self.assertTrue(ledger.reconcile()['balanced'])
        finally:
            ledger.close()
        ledger = PaperWalletLedger(self.path)
        try:
            after = tuple(ledger.db.execute("SELECT fingerprint,details FROM ledger_events WHERE id='btc-history'").fetchone())
            self.assertEqual(before, after)
            self.assertEqual(ledger.balance('spot:position','SOL'), Decimal('1.234'))
            self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM ledger_events WHERE id='sol-event'").fetchone()[0],1)
            self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM ledger_events WHERE kind='OPENING'").fetchone()[0],0)
            self.assertTrue(ledger.reconcile()['balanced'])
        finally:
            ledger.close()

    def test_negative_third_asset_in_free_or_reserved_fails_reconcile(self):
        ledger = PaperWalletLedger(self.path)
        try:
            for bucket in ('free','reserved'):
                with self.subTest(bucket=bucket):
                    account='spot:'+bucket
                    with ledger.transaction():
                        ledger._post('negative-'+bucket,'FIXTURE',{},[(account,'SOL','-0.001'),('external:fixture','SOL','0.001')])
                    result=ledger.reconcile()
                    self.assertFalse(result['balanced'])
                    self.assertIn(account,result['negative_accounts'])
                    with ledger.transaction():
                        ledger._post('restore-'+bucket,'FIXTURE',{},[(account,'SOL','0.001'),('external:fixture','SOL','-0.001')])
                    self.assertTrue(ledger.reconcile()['balanced'])
        finally:
            ledger.close()


if __name__ == '__main__':
    unittest.main()
