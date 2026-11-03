import tempfile
import sqlite3
import subprocess
import sys
import time
import unittest
from decimal import Decimal
from pathlib import Path

from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.paper_wallet_ledger import PaperWalletLedger


class MarginPaperBookTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ledger=PaperWalletLedger(Path(self.tmp.name)/'ledger.db');self.addCleanup(self.ledger.close)
        self.book=MarginPaperBook(self.ledger)
        with self.ledger.transaction():
            self.ledger._post('seed','FIXTURE',{},[('margin_cross:free','USDT',Decimal(50)),('external:fixture','USDT',Decimal(-50))])
            self.ledger.db.execute("UPDATE wallet_state SET paused=0 WHERE wallet='margin_cross'")
        f={'lot_rules':[{'min':'0','max':'0','step':'0.00000000000000000001'}],'min_notional':'0','max_notional':'0','observed_ms':1000}
        self.quotes={'BTCUSDT':{'price':'100','time_ms':1000,'server_ms':1000,'source':'fixture','price_kind':'last_public_trade','filters':f},'ETHUSDT':{'price':'200','time_ms':1000,'server_ms':1000,'source':'fixture','price_kind':'last_public_trade','filters':f}}
        self.book.record_quotes(self.quotes,now_ms=1000)

    def test_long_and_short_open_and_close_with_separate_debts(self):
        initial=self.book.risk(self.quotes,now_ms=1000)['equity']
        long=self.book.open('long','p1','BTCUSDT','LONG','.09','5',2,self.quotes,now_ms=1000)
        short=self.book.open('short','p2','ETHUSDT','SHORT','.04','5',2,self.quotes,now_ms=1000)
        self.assertEqual(self.book.open('long','p1','BTCUSDT','LONG','.09','5',2,self.quotes,now_ms=1000),long)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM margin_positions').fetchone()[0],2)
        debts={r['asset']:Decimal(r['principal']) for r in self.ledger.db.execute('SELECT asset,principal FROM margin_loans')}
        self.assertGreater(debts['USDT'],0);self.assertEqual(debts['ETH'],Decimal('.04'))
        self.assertLess(self.book.risk(self.quotes,now_ms=1000)['equity'],initial)
        with self.assertRaisesRegex(ValueError,'Position changed'):
            self.book.close('stale-close','BTCUSDT','p1','.08',self.quotes,now_ms=1000)
        a=self.book.close('close-long','BTCUSDT','p1','.09',self.quotes,now_ms=1000)
        b=self.book.close('close-short','ETHUSDT','p2','.04',self.quotes,now_ms=1000)
        self.assertEqual(Decimal(a['remaining_debt']),0);self.assertEqual(Decimal(b['remaining_debt']),0)
        self.assertEqual(self.book.close('close-short','ETHUSDT','p2','.04',self.quotes,now_ms=1000),b)
        self.assertEqual(self.book.close('close-short','ETHUSDT','p2','.04',{},now_ms=500000,expires_ms=2000,clock_ms=lambda:500000),b)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM margin_loans WHERE status='OPEN'").fetchone()[0],0)
        self.assertTrue(self.ledger.reconcile()['balanced'])

    def test_stale_quotes_and_leverage_budget_block_entry(self):
        with self.assertRaisesRegex(ValueError,'stale'):
            self.book.open('a','p','BTCUSDT','LONG','.09','5',2,self.quotes,now_ms=200000)
        with self.assertRaisesRegex(ValueError,'budget'):
            self.book.open('b','p','BTCUSDT','LONG','.2','5',2,self.quotes,now_ms=1000)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM margin_positions').fetchone()[0],0)

    def test_stale_filters_block_trade_but_not_risk_calculation(self):
        quotes={s:{**q,'time_ms':200000,'server_ms':200000} for s,q in self.quotes.items()}
        self.book.record_quotes(quotes,now_ms=200000)
        self.assertEqual(self.book.risk(quotes,now_ms=200000)['equity'],Decimal(50))
        with self.assertRaisesRegex(ValueError,'stale Spot filters'):
            self.book.open('a','p','BTCUSDT','LONG','.09','5',2,quotes,now_ms=200000)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM margin_positions').fetchone()[0],0)

    def test_superseded_quote_cannot_authorize_entry(self):
        newer={s:{**q,'time_ms':2000,'server_ms':2000} for s,q in self.quotes.items()}
        self.book.record_quotes(newer,now_ms=2000)
        with self.assertRaisesRegex(ValueError,'superseded'):
            self.book.open('old','p','BTCUSDT','LONG','.09','5',2,self.quotes,now_ms=2000)
        self.assertEqual(self.ledger.balance('margin_cross:free'),Decimal(50))
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM margin_positions').fetchone()[0],0)

    def test_short_repayment_uses_exact_exchange_lot_and_preserves_dust(self):
        filters={'lot_rules':[{'min':'.001','max':'100','step':'.001'}],'min_notional':'0','max_notional':'0','observed_ms':2000}
        quotes={s:{**q,'time_ms':2000,'server_ms':2000,'filters':filters} for s,q in self.quotes.items()}
        self.book.record_quotes(quotes,now_ms=2000)
        self.book.open('short','p','ETHUSDT','SHORT','.04','5',2,quotes,now_ms=2000)
        result=self.book.close('close','ETHUSDT','p','.04',quotes,now_ms=2000)
        self.assertEqual(Decimal(result['remaining_debt']),0)
        self.assertEqual(self.ledger.balance('margin_cross:free','ETH'),Decimal('.0009996'))
        self.assertTrue(self.ledger.reconcile()['balanced'])

    def test_equity_changes_only_by_explicit_long_costs_at_fixed_mark(self):
        before=self.book.risk(self.quotes,now_ms=1000)['equity']
        self.book.open('long','p','BTCUSDT','LONG','.09','5',2,self.quotes,now_ms=1000)
        after_open=self.book.risk(self.quotes,now_ms=1000)['equity']
        entry_slippage=Decimal('.09')*Decimal('.02')
        entry_fee=Decimal('.09')*Decimal('100.02')*Decimal('.001')
        principal=Decimal('.09')*Decimal('100.02')+entry_fee-Decimal(5)
        interest=principal*Decimal('.00001')
        self.assertEqual(before-after_open,entry_slippage+entry_fee+interest)
        self.book.close('close','BTCUSDT','p','.09',self.quotes,now_ms=1000)
        after_close=self.book.risk(self.quotes,now_ms=1000)['equity']
        exit_slippage=Decimal('.09')*Decimal('.02')
        exit_fee=Decimal('.09')*Decimal('99.98')*Decimal('.001')
        self.assertEqual(before-after_close,entry_slippage+exit_slippage+entry_fee+exit_fee+interest)

    def test_liquidation_preserves_deficit_and_other_wallet(self):
        with self.ledger.transaction():
            self.ledger._post('withdraw-fixture','FIXTURE',{},[('margin_cross:free','USDT',Decimal(-45)),('external:fixture','USDT',Decimal(45))])
            self.ledger._post('other-wallet','FIXTURE',{},[('futures_isolated:free','USDT',Decimal(20)),('external:fixture','USDT',Decimal(-20))])
        self.book.open('long','p','BTCUSDT','LONG','.09','5',2,self.quotes,now_ms=1000)
        crash={s:{**q,'price':'1' if s=='BTCUSDT' else '200','time_ms':2000,'server_ms':2000} for s,q in self.quotes.items()}
        self.book.record_quotes(crash,now_ms=2000)
        self.assertLess(self.book.risk(crash,now_ms=2000)['equity'],0)
        result=self.book.liquidate('liq',crash,now_ms=2000)
        self.assertGreater(Decimal(result['positions'][0]['remaining_debt']),0)
        self.assertEqual(self.ledger.balance('futures_isolated:free'),20)
        self.assertEqual(self.ledger.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0],1)
        self.assertLess(self.ledger.balance('margin_cross:principal'),0)
        self.assertTrue(self.ledger.reconcile()['balanced'])

    def test_short_liquidation_rebuys_only_affordable_asset_and_retains_debt(self):
        with self.ledger.transaction():
            self.ledger._post('reduce-fixture','FIXTURE',{},[('margin_cross:free','USDT',Decimal(-45)),('external:fixture','USDT',Decimal(45))])
        self.book.open('short','p','BTCUSDT','SHORT','.09','5',2,self.quotes,now_ms=1000)
        shock={s:{**q,'price':'1000' if s=='BTCUSDT' else '200','time_ms':2000,'server_ms':2000} for s,q in self.quotes.items()}
        self.book.record_quotes(shock,now_ms=2000)
        self.assertLess(self.book.risk(shock,now_ms=2000)['equity'],0)
        result=self.book.liquidate('liq-short',shock,now_ms=2000)
        self.assertGreater(Decimal(result['positions'][0]['remaining_debt']),0)
        self.assertLess(self.ledger.balance('margin_cross:principal','BTC'),0)
        self.assertEqual(self.ledger.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0],1)
        self.assertTrue(self.ledger.reconcile()['balanced'])

    def test_competing_entries_cannot_spend_same_collateral(self):
        with self.ledger.transaction():
            self.ledger._post('reduce-fixture','FIXTURE',{},[('margin_cross:free','USDT',Decimal(-45)),('external:fixture','USDT',Decimal(45))])
        script='''import sqlite3,sys
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.paper_wallet_ledger import PaperWalletLedger
db=sqlite3.connect(sys.argv[1],isolation_level=None,timeout=15);db.row_factory=sqlite3.Row
db.execute("PRAGMA foreign_keys=ON");db.execute("PRAGMA synchronous=FULL")
l=object.__new__(PaperWalletLedger);l.db=db;l.fault=lambda point:None
b=MarginPaperBook(l);symbol=sys.argv[2];q=".09" if symbol=="BTCUSDT" else ".04"
f={'lot_rules':[{'min':'0','max':'0','step':'0.00000000000000000001'}],'min_notional':'0','max_notional':'0','observed_ms':1000}
quotes={"BTCUSDT":{"price":"100","time_ms":1000,"server_ms":1000,"source":"fixture","price_kind":"last_public_trade","filters":f},"ETHUSDT":{"price":"200","time_ms":1000,"server_ms":1000,"source":"fixture","price_kind":"last_public_trade","filters":f}}
try:b.open(symbol,symbol,symbol,"LONG",q,"5",2,quotes,now_ms=1000)
except ValueError:sys.exit(1)
'''
        path=str(Path(self.tmp.name)/'ledger.db')
        jobs=[subprocess.Popen([sys.executable,'-B','-c',script,path,s],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for s in ('BTCUSDT','ETHUSDT')]
        codes=[]
        for job in jobs:
            _,err=job.communicate(timeout=30);codes.append(job.returncode)
            if job.returncode not in (0,1):self.fail(err.decode())
        self.assertEqual(sorted(codes),[0,1])
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM margin_positions').fetchone()[0],1)
        self.assertTrue(self.ledger.reconcile()['balanced'])

    def test_process_death_does_not_split_trade_and_borrow(self):
        self.ledger.close();path=str(Path(self.tmp.name)/'ledger.db')
        script='''import os,sqlite3,sys
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.paper_wallet_ledger import PaperWalletLedger
db=sqlite3.connect(sys.argv[1],isolation_level=None);db.row_factory=sqlite3.Row
db.execute("PRAGMA foreign_keys=ON");db.execute("PRAGMA synchronous=FULL")
l=object.__new__(PaperWalletLedger);l.db=db;l.fault=lambda p:None
b=MarginPaperBook(l);l.fault=lambda point:os._exit(73) if point==sys.argv[2] else None
f={'lot_rules':[{'min':'0','max':'0','step':'0.00000000000000000001'}],'min_notional':'0','max_notional':'0','observed_ms':1000}
q={"BTCUSDT":{"price":"100","time_ms":1000,"server_ms":1000,"source":"fixture","price_kind":"last_public_trade","filters":f},"ETHUSDT":{"price":"200","time_ms":1000,"server_ms":1000,"source":"fixture","price_kind":"last_public_trade","filters":f}}
b.open("one","p","BTCUSDT","LONG",".09","5",2,q,now_ms=1000)
'''
        for point,expected in (('before_commit',0),('after_commit',1)):
            child=subprocess.run([sys.executable,'-B','-c',script,path,point],timeout=30)
            self.assertEqual(child.returncode,73)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM margin_positions').fetchone()[0],expected)
                self.assertEqual(db.execute('SELECT count(*) FROM margin_loans').fetchone()[0],expected)
        child=subprocess.run([sys.executable,'-B','-c',script,path,'never'],timeout=30)
        self.assertEqual(child.returncode,0)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM margin_positions').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM ledger_events WHERE kind='MARGIN_TRADE'").fetchone()[0],1)

    def test_process_death_does_not_split_close_and_repayment(self):
        self.book.open('one','p','BTCUSDT','LONG','.09','5',2,self.quotes,now_ms=1000)
        self.ledger.close();path=str(Path(self.tmp.name)/'ledger.db')
        script='''import os,sqlite3,sys
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.paper_wallet_ledger import PaperWalletLedger
db=sqlite3.connect(sys.argv[1],isolation_level=None);db.row_factory=sqlite3.Row
db.execute("PRAGMA foreign_keys=ON");db.execute("PRAGMA synchronous=FULL")
l=object.__new__(PaperWalletLedger);l.db=db;l.fault=lambda p:None
b=MarginPaperBook(l);l.fault=lambda point:os._exit(73) if point==sys.argv[2] else None
f={'lot_rules':[{'min':'0','max':'0','step':'0.00000000000000000001'}],'min_notional':'0','max_notional':'0','observed_ms':1000}
q={"BTCUSDT":{"price":"100","time_ms":1000,"server_ms":1000,"source":"fixture","price_kind":"last_public_trade","filters":f},"ETHUSDT":{"price":"200","time_ms":1000,"server_ms":1000,"source":"fixture","price_kind":"last_public_trade","filters":f}}
b.close("close","BTCUSDT","p",".09",q,now_ms=1000)
'''
        for point,positions,status in (('before_commit',1,'OPEN'),('after_commit',0,'CLOSED')):
            child=subprocess.run([sys.executable,'-B','-c',script,path,point],timeout=30)
            self.assertEqual(child.returncode,73)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM margin_positions').fetchone()[0],positions)
                self.assertEqual(db.execute('SELECT status FROM margin_loans').fetchone()[0],status)
        self.assertEqual(subprocess.run([sys.executable,'-B','-c',script,path,'never'],timeout=30).returncode,0)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM ledger_events WHERE kind='MARGIN_REPAY'").fetchone()[0],1)

    def test_missing_position_asset_blocks_new_work(self):
        self.book.open('long','p','BTCUSDT','LONG','.09','5',2,self.quotes,now_ms=1000)
        with self.ledger.transaction():
            self.ledger._post('corrupt-fixture','FIXTURE',{},[('margin_cross:free','BTC',Decimal('-.01')),('external:fixture','BTC',Decimal('.01'))])
        with self.assertRaisesRegex(ValueError,'exceeds accounted asset'):
            self.book.risk(self.quotes,now_ms=1000)

    def test_position_shadow_conflict_blocks_new_work(self):
        self.book.open('long','p','BTCUSDT','LONG','.09','5',2,self.quotes,now_ms=1000)
        with self.ledger.transaction():
            self.ledger.db.execute("UPDATE margin_positions SET quantity='.08' WHERE id='p'")
        with self.assertRaisesRegex(ValueError,'position journal mismatch'):
            self.book.risk(self.quotes,now_ms=1000)

    def test_close_confirmation_expires_while_waiting_for_real_lock(self):
        self.book.open('long','p','BTCUSDT','LONG','.09','5',2,self.quotes,now_ms=1000)
        wall=int(time.time()*1000);blocker=sqlite3.connect(Path(self.tmp.name)/'ledger.db',isolation_level=None,timeout=5)
        self.addCleanup(blocker.close)
        script='''import sqlite3,sys,time
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.paper_wallet_ledger import PaperWalletLedger
db=sqlite3.connect(sys.argv[1],isolation_level=None,timeout=10);db.row_factory=sqlite3.Row
db.execute("PRAGMA foreign_keys=ON");db.execute("PRAGMA synchronous=FULL")
l=object.__new__(PaperWalletLedger);l.db=db;l.fault=lambda p:None
b=MarginPaperBook(l);q=b.stored_quotes();print("ready",flush=True);sys.stdin.readline()
try:b.close("close","BTCUSDT","p",".09",q,now_ms=1000,expires_ms=2000,clock_ms=lambda:1000+int(time.time()*1000)-int(sys.argv[2]))
except ValueError as e:
 if "expired" in str(e):sys.exit(0)
 raise
sys.exit(1)
'''
        job=subprocess.Popen([sys.executable,'-B','-c',script,str(Path(self.tmp.name)/'ledger.db'),str(wall)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.assertEqual(job.stdout.readline().strip(),b'ready')
        blocker.execute('BEGIN IMMEDIATE');job.stdin.write(b'go\n');job.stdin.flush()
        time.sleep(1.2);blocker.rollback()
        _,err=job.communicate(timeout=20);self.assertEqual(job.returncode,0,err.decode())
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM margin_positions').fetchone()[0],1)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM ledger_events WHERE id='margin:close:close'").fetchone()[0],0)


if __name__=='__main__':unittest.main()
