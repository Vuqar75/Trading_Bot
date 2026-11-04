import sqlite3
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from trading_bot.margin_interest_ledger import HOUR_MS, MarginInterestLedger
from trading_bot.paper_wallet_ledger import PaperWalletLedger


class MarginInterestLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ledger.db'
        self.ledger=PaperWalletLedger(self.path);self.addCleanup(self.ledger.close)
        self.book=MarginInterestLedger(self.ledger)

    def seed(self,asset,value):
        value=Decimal(value)
        with self.ledger.transaction():
            self.ledger._post('seed:'+asset,'FIXTURE',{},[('margin_cross:free',asset,value),('external:fixture',asset,-value)])

    def reopen(self):
        self.ledger.close()
        db=sqlite3.connect(self.path,isolation_level=None);db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA synchronous=FULL')
        ledger=object.__new__(PaperWalletLedger);ledger.db=db;ledger.fault=lambda point:None
        self.addCleanup(ledger.close)
        return ledger,MarginInterestLedger(ledger)

    def test_borrow_and_repay_principal_are_not_income_or_loss(self):
        self.book.borrow('b1','l1','position','USDT','100',now_ms=100)
        self.assertEqual(self.ledger.balance('margin_cross:free'),Decimal('100'))
        self.assertEqual(self.ledger.balance('margin_cross:principal'),Decimal('-100'))
        self.assertEqual(self.ledger.balance('margin_cross:interest'),Decimal('-.001'))
        self.seed('USDT','.001')
        paid=self.book.repay('r1','l1','100.001',now_ms=100)
        self.assertEqual(Decimal(paid['interest']),Decimal('.001'))
        self.assertEqual(Decimal(paid['principal']),Decimal('100'))
        self.assertEqual(self.ledger.balance('margin_cross:free'),0)
        self.assertEqual(self.ledger.balance('margin_cross:principal'),0)
        self.assertEqual(self.ledger.balance('margin_cross:interest'),0)
        self.assertEqual(self.book.repay('r1','l1','100.001',now_ms=100),paid)
        self.assertTrue(self.ledger.reconcile()['balanced'])

    def test_period_boundaries_rates_and_partial_principal(self):
        self.book.borrow('b','l','p','BTC','1',now_ms=10)
        self.assertEqual(self.book.accrue('l',now_ms=HOUR_MS+9),[])
        self.seed('BTC','.00001')
        result=self.book.repay('r','l','.50001',now_ms=HOUR_MS-1)
        self.assertEqual(Decimal(result['remaining_principal']),Decimal('.5'))
        self.book.set_rate('BTC','.00002',effective_ms=HOUR_MS+10)
        records=self.book.accrue('l',now_ms=HOUR_MS+10)
        self.assertEqual(len(records),1)
        self.assertEqual(Decimal(records[0]['principal']),Decimal('.5'))
        self.assertEqual(Decimal(records[0]['rate']),Decimal('.00002'))
        self.assertEqual(Decimal(records[0]['charge']),Decimal('.00001'))
        self.assertEqual(self.book.accrue('l',now_ms=HOUR_MS+10),[])
        self.assertEqual(self.ledger.balance('margin_cross:interest','BTC'),Decimal('-.00001'))
        with self.assertRaisesRegex(ValueError,'rewrite history'):
            self.book.set_rate('BTC','.00003',effective_ms=HOUR_MS)

    def test_two_loans_two_assets_insufficient_free_and_restart(self):
        self.book.borrow('b1','btc','p1','BTC','2',now_ms=50)
        self.book.borrow('b2','eth','p2','ETH','3',now_ms=50)
        with self.assertRaisesRegex(ValueError,'Insufficient free'):
            self.book.repay('r','btc','2.00002',now_ms=50)
        ledger,book=self.reopen()
        book.accrue('btc',now_ms=50+2*HOUR_MS);book.accrue('eth',now_ms=50+2*HOUR_MS)
        self.assertEqual(ledger.db.execute('SELECT count(*) FROM margin_interest_periods').fetchone()[0],6)
        self.assertEqual(ledger.balance('margin_cross:interest','BTC'),Decimal('-.00006'))
        self.assertEqual(ledger.balance('margin_cross:interest','ETH'),Decimal('-.00009'))
        self.assertTrue(ledger.reconcile()['balanced'])

    def test_process_death_before_and_after_commit(self):
        self.ledger.close()
        script='''import os,sqlite3,sys
from trading_bot.margin_interest_ledger import MarginInterestLedger
from trading_bot.paper_wallet_ledger import PaperWalletLedger
db=sqlite3.connect(sys.argv[1],isolation_level=None);db.row_factory=sqlite3.Row
db.execute("PRAGMA foreign_keys=ON");db.execute("PRAGMA synchronous=FULL")
l=object.__new__(PaperWalletLedger);l.db=db;l.fault=lambda p:None
b=MarginInterestLedger(l)
l.fault=lambda point:os._exit(73) if point==sys.argv[2] else None
b.borrow("operation","loan","position","USDT","10",now_ms=100)
'''
        for point,expected in [('before_commit',0),('after_commit',1)]:
            child=subprocess.run([sys.executable,'-B','-c',script,str(self.path),point],timeout=30)
            self.assertEqual(child.returncode,73)
            with sqlite3.connect(self.path) as db:self.assertEqual(db.execute('SELECT count(*) FROM margin_loans').fetchone()[0],expected)
        replay=subprocess.run([sys.executable,'-B','-c',script,str(self.path),'never'],timeout=30)
        self.assertEqual(replay.returncode,0)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM margin_loans').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM margin_interest_periods').fetchone()[0],1)

    def test_concurrent_accrual_counts_each_period_once(self):
        self.book.borrow('b','loan','position','USDT','100',now_ms=100)
        script='''import sqlite3,sys
from trading_bot.margin_interest_ledger import MarginInterestLedger,HOUR_MS
from trading_bot.paper_wallet_ledger import PaperWalletLedger
db=sqlite3.connect(sys.argv[1],isolation_level=None,timeout=15);db.row_factory=sqlite3.Row
db.execute("PRAGMA foreign_keys=ON");db.execute("PRAGMA synchronous=FULL")
l=object.__new__(PaperWalletLedger);l.db=db;l.fault=lambda p:None
MarginInterestLedger(l).accrue("loan",now_ms=100+5*HOUR_MS)
'''
        jobs=[subprocess.Popen([sys.executable,'-B','-c',script,str(self.path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
        for job in jobs:
            _,err=job.communicate(timeout=30)
            self.assertEqual(job.returncode,0,err.decode())
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM margin_interest_periods WHERE loan_id=?',('loan',)).fetchone()[0],6)
        self.assertEqual(self.ledger.balance('margin_cross:interest'),Decimal('-.006'))
        self.assertTrue(self.ledger.reconcile()['balanced'])

    def test_shadow_disagreement_blocks_further_loan_operations(self):
        self.book.borrow('b','loan','position','USDT','10',now_ms=100)
        with self.ledger.transaction():
            self.ledger.db.execute("UPDATE margin_loans SET principal='9' WHERE id='loan'")
        with self.assertRaisesRegex(ValueError,'disagree'):
            self.book.accrue('loan',now_ms=100+HOUR_MS)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM margin_interest_periods').fetchone()[0],1)


if __name__=='__main__':unittest.main()
