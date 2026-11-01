import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from trading_bot.paper_wallet_ledger import PaperWalletLedger


class WalletLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'paper-ledger.db'
        self.ledger=PaperWalletLedger(self.path);self.addCleanup(lambda:self.ledger.close())
        self.positions={'spot':{'id':'spot-1','quantity':'0.01','cost':'700','fees_known':True},
                        'futures':{'id':'fut-1','direction':'SHORT','quantity':'0.0001','margin':'10','funding':'0.002'}}
        self.args=dict(spot_cash='300',spot_btc='0.01',futures_cash='990',futures_margin='10',funding='0.002',position_metadata=self.positions,source='snapshot:sha256:example',captured_at=123)
        self.ledger.import_snapshot(**self.args)

    def test_import_preserves_balances_and_has_no_second_initial_credit(self):
        self.assertEqual(self.ledger.balance('spot:free'),Decimal('300'))
        self.assertEqual(self.ledger.balance('spot:position','BTC'),Decimal('.01'))
        self.assertEqual(self.ledger.balance('futures_isolated:free'),Decimal('990'))
        self.assertEqual(self.ledger.balance('futures_isolated:margin'),Decimal('10'))
        self.assertEqual(self.ledger.balance('futures_isolated:funding'),Decimal('.002'))
        self.assertEqual(self.ledger.balance('reserve:free'),Decimal(0))
        self.assertTrue(self.ledger.reconcile()['balanced'])
        with self.assertRaises(ValueError):self.ledger.import_snapshot(**self.args)

    def test_transfer_confirmation_once_and_capital_conservation(self):
        p=self.ledger.propose_transfer('update:1','spot','futures_isolated','50',now=10)
        self.assertEqual((p['source_after'],p['destination_after']),('250','1040'))
        again=self.ledger.propose_transfer('update:1','spot','futures_isolated','50',now=11)
        self.assertEqual((again['id'],again['source_after'],again['destination_after']),(p['id'],'250','1040'))
        self.assertEqual(self.ledger.confirm_transfer(p['id'],now=11),self.ledger.confirm_transfer(p['id'],now=12))
        self.assertEqual(self.ledger.balance('spot:free'),Decimal('250'))
        self.assertEqual(self.ledger.balance('futures_isolated:free'),Decimal('1040'))
        self.assertEqual(self.ledger.balance('futures_isolated:margin'),Decimal('10'))
        self.assertEqual(self.ledger.db.execute("select count(*) from ledger_events where kind='TRANSFER'").fetchone()[0],1)
        self.assertTrue(self.ledger.reconcile()['balanced'])

    def test_insufficient_paused_and_expiry_and_cancel(self):
        self.ledger.set_paused('spot',False)
        with self.assertRaises(ValueError):self.ledger.propose_transfer('one','spot','reserve','1')
        self.ledger.set_paused('spot',True)
        with self.assertRaises(ValueError):self.ledger.propose_transfer('two','spot','reserve','301')
        p=self.ledger.propose_transfer('three','spot','reserve','100',now=1,ttl=10)
        with self.assertRaises(ValueError):self.ledger.confirm_transfer(p['id'],now=11)
        p=self.ledger.propose_transfer('four','spot','reserve','100',now=1)
        self.ledger.cancel_transfer(p['id'],now=2)
        with self.assertRaises(ValueError):self.ledger.confirm_transfer(p['id'],now=3)
        self.assertEqual(self.ledger.balance('spot:free'),Decimal('300'))

    def test_confirm_rechecks_reserves_and_pause(self):
        p=self.ledger.propose_transfer('a','spot','reserve','200',now=1)
        self.ledger.move_reserve('order-a','spot','150')
        with self.assertRaises(ValueError):self.ledger.confirm_transfer(p['id'],now=2)
        self.ledger.move_reserve('release-a','spot','150',release=True)
        self.ledger.set_paused('spot',False)
        with self.assertRaises(ValueError):self.ledger.confirm_transfer(p['id'],now=3)
        self.ledger.set_paused('spot',True);self.ledger.confirm_transfer(p['id'],now=4)
        self.assertEqual(self.ledger.balance('spot:free'),Decimal('100'))

    def test_conflicting_identity_and_namespace(self):
        p=self.ledger.propose_transfer('u:9','spot','reserve','1')
        self.assertTrue(p['id'].startswith('T-'))
        with self.assertRaises(ValueError):self.ledger.propose_transfer('u:9','spot','reserve','2')
        with self.assertRaises(ValueError):self.ledger.confirm_transfer('position-close-id')

    def test_crash_after_commit_and_restart_does_not_duplicate(self):
        p=self.ledger.propose_transfer('u:8','spot','reserve','50',now=1)
        code=("import os,sys; from trading_bot.paper_wallet_ledger import PaperWalletLedger; "
              "r=PaperWalletLedger(sys.argv[1]); r.fault=lambda p: os._exit(73) if p=='after_commit' else None; "
              "r.confirm_transfer(sys.argv[2],now=2)")
        result=subprocess.run([sys.executable,'-c',code,str(self.path),p['id']])
        self.assertEqual(result.returncode,73)
        self.ledger.close();self.ledger=PaperWalletLedger(self.path)
        self.ledger.confirm_transfer(p['id'],now=3)
        self.assertEqual(self.ledger.balance('reserve:free'),Decimal('50'))
        self.assertEqual(self.ledger.db.execute("select count(*) from ledger_events where kind='TRANSFER'").fetchone()[0],1)

    def test_crash_before_commit_rolls_back(self):
        p=self.ledger.propose_transfer('u:7','spot','reserve','50',now=1)
        def fail(point):
            if point=='before_commit':raise RuntimeError('failure')
        self.ledger.fault=fail
        with self.assertRaises(RuntimeError):self.ledger.confirm_transfer(p['id'],now=2)
        self.ledger.fault=lambda _:None
        self.assertEqual(self.ledger.balance('reserve:free'),Decimal('0'))
        self.ledger.confirm_transfer(p['id'],now=3)
        self.assertEqual(self.ledger.balance('reserve:free'),Decimal('50'))

    def test_process_death_before_commit_rolls_back_after_restart(self):
        p=self.ledger.propose_transfer('u:crash-before','spot','reserve','50',now=1)
        code=("import os,sys; from trading_bot.paper_wallet_ledger import PaperWalletLedger; "
              "r=PaperWalletLedger(sys.argv[1]); r.fault=lambda p: os._exit(74) if p=='before_commit' else None; "
              "r.confirm_transfer(sys.argv[2],now=2)")
        result=subprocess.run([sys.executable,'-c',code,str(self.path),p['id']])
        self.assertEqual(result.returncode,74)
        self.ledger.close();self.ledger=PaperWalletLedger(self.path)
        self.assertEqual(self.ledger.balance('reserve:free'),Decimal(0))
        self.ledger.confirm_transfer(p['id'],now=3)
        self.assertEqual(self.ledger.balance('reserve:free'),Decimal('50'))

    def test_concurrent_reservation_or_transfer_cannot_overspend(self):
        p=self.ledger.propose_transfer('u:5','spot','reserve','200',now=1)
        barrier=threading.Barrier(2);results=[]
        def run(kind):
            ledger=PaperWalletLedger(self.path)
            try:
                barrier.wait()
                try:
                    if kind=='reserve':ledger.move_reserve('race-reserve','spot','200')
                    else:ledger.confirm_transfer(p['id'],now=2)
                    results.append('ok')
                except ValueError:results.append('rejected')
            finally:ledger.close()
        ts=[threading.Thread(target=run,args=(kind,)) for kind in ('reserve','transfer')]
        for t in ts:t.start()
        for t in ts:t.join()
        self.assertCountEqual(results,['ok','rejected'])
        self.assertGreaterEqual(self.ledger.balance('spot:free'),0)
        self.assertTrue(self.ledger.reconcile()['balanced'])

    def test_immutable_postings_and_mismatched_import_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):self.ledger.db.execute('delete from ledger_postings')
        with self.assertRaises(sqlite3.IntegrityError):self.ledger.db.execute("insert into ledger_postings values('opening:1',99,'reserve:free','USDT','1')")
        other=PaperWalletLedger(Path(self.tmp.name)/'other.db');self.addCleanup(other.close)
        with self.assertRaises(ValueError):other.import_snapshot(**{**self.args,'futures_margin':'9'})
        self.assertEqual(other.balance('spot:free'),0)
