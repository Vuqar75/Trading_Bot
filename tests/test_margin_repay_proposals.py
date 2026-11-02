import sqlite3
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from trading_bot.margin_repay_proposals import MarginRepayProposals
from trading_bot.paper_wallet_ledger import PaperWalletLedger


class RepayProposalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ledger.db'
        self.ledger=PaperWalletLedger(self.path);self.addCleanup(self.ledger.close)
        self.proposals=MarginRepayProposals(self.ledger)
        self.proposals.loans.borrow('b','loan','position','BTC','1',now_ms=100)

    def test_typed_confirmation_is_atomic_and_idempotent(self):
        p=self.proposals.propose('request','loan','.5',now_ms=101)
        self.assertTrue(p['id'].startswith('R-'))
        self.assertEqual(self.proposals.propose('request','loan','.5',now_ms=103)['id'],p['id'])
        with self.assertRaisesRegex(ValueError,'Not a repayment'):
            self.proposals.confirm('T-'+p['id'][2:],now_ms=102)
        result=self.proposals.confirm(p['id'],now_ms=102)
        self.assertEqual(self.proposals.confirm(p['id'],now_ms=103),result)
        self.assertEqual(Decimal(result['interest']),Decimal('.00001'))
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM ledger_events WHERE kind='MARGIN_REPAY'").fetchone()[0],1)
        self.assertTrue(self.ledger.reconcile()['balanced'])

    def test_stale_cancel_expiry_and_insufficient_asset(self):
        p=self.proposals.propose('request','loan','.5',now_ms=101)
        self.proposals.loans.repay('other','loan','.1',now_ms=102)
        with self.assertRaisesRegex(ValueError,'Loan changed'):
            self.proposals.confirm(p['id'],now_ms=103)
        self.proposals.cancel(p['id'],now_ms=104);self.proposals.cancel(p['id'],now_ms=105)
        with self.assertRaisesRegex(ValueError,'expired or canceled'):
            self.proposals.confirm(p['id'],now_ms=106)
        exp=self.proposals.propose('exp','loan','.1',now_ms=110,ttl_ms=5)
        with self.assertRaisesRegex(ValueError,'expired or canceled'):
            self.proposals.confirm(exp['id'],now_ms=115)
        with self.assertRaisesRegex(ValueError,'Insufficient free'):
            self.proposals.propose('too-much','loan','.90001',now_ms=116)

    def test_process_death_before_and_after_confirmation_commit(self):
        token=self.proposals.propose('request','loan','.5',now_ms=101)['id']
        self.ledger.close()
        script='''import os,sqlite3,sys
from trading_bot.margin_repay_proposals import MarginRepayProposals
from trading_bot.paper_wallet_ledger import PaperWalletLedger
db=sqlite3.connect(sys.argv[1],isolation_level=None);db.row_factory=sqlite3.Row
db.execute("PRAGMA foreign_keys=ON");db.execute("PRAGMA synchronous=FULL")
l=object.__new__(PaperWalletLedger);l.db=db;l.fault=lambda p:None
p=MarginRepayProposals(l);l.fault=lambda point:os._exit(73) if point==sys.argv[3] else None
p.confirm(sys.argv[2],now_ms=102)
'''
        for point,status,count in (('before_commit','OPEN',0),('after_commit','DONE',1)):
            child=subprocess.run([sys.executable,'-B','-c',script,str(self.path),token,point],timeout=30)
            self.assertEqual(child.returncode,73)
            with sqlite3.connect(self.path) as db:
                self.assertEqual(db.execute('SELECT status FROM margin_repay_proposals').fetchone()[0],status)
                self.assertEqual(db.execute("SELECT count(*) FROM ledger_events WHERE kind='MARGIN_REPAY'").fetchone()[0],count)
        child=subprocess.run([sys.executable,'-B','-c',script,str(self.path),token,'never'],timeout=30)
        self.assertEqual(child.returncode,0)
        with sqlite3.connect(self.path) as db:self.assertEqual(db.execute("SELECT count(*) FROM ledger_events WHERE kind='MARGIN_REPAY'").fetchone()[0],1)

    def test_concurrent_confirmations_do_not_double_repay(self):
        token=self.proposals.propose('request','loan','.5',now_ms=101)['id']
        script='''import sqlite3,sys
from trading_bot.margin_repay_proposals import MarginRepayProposals
from trading_bot.paper_wallet_ledger import PaperWalletLedger
db=sqlite3.connect(sys.argv[1],isolation_level=None,timeout=15);db.row_factory=sqlite3.Row
db.execute("PRAGMA foreign_keys=ON");db.execute("PRAGMA synchronous=FULL")
l=object.__new__(PaperWalletLedger);l.db=db;l.fault=lambda p:None
MarginRepayProposals(l).confirm(sys.argv[2],now_ms=102)
'''
        jobs=[subprocess.Popen([sys.executable,'-B','-c',script,str(self.path),token],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
        for job in jobs:
            _,err=job.communicate(timeout=30)
            self.assertEqual(job.returncode,0,err.decode())
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM ledger_events WHERE kind='MARGIN_REPAY'").fetchone()[0],1)
        self.assertTrue(self.ledger.reconcile()['balanced'])


if __name__=='__main__':unittest.main()
