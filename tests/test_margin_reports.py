import json
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from trading_bot.cross_command_receiver import CrossCommandReceiver
from trading_bot.cross_paper_book import CrossPaperBook
from trading_bot.cross_worker import process_cross
from trading_bot.futures_store import FuturesStore
from trading_bot.margin_command_receiver import MarginCommandReceiver
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.margin_worker import process_margin
from trading_bot.paper_consolidation import consolidate_snapshots
from trading_bot.paper_shared_store import SharedPaperStore
from trading_bot.runtime_store import RuntimeStore
from trading_bot.unified_queue import UnifiedQueue
from trading_bot.unified_worker import process_market


class MarginReportsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        spot=RuntimeStore(root/'spot.db',{'mode':'paper','interval':'15m'});future=FuturesStore(root/'future.db')
        spot.close();future.close();consolidate_snapshots(root/'spot.db',root/'future.db',root/'shared.db',captured_at=1)
        self.shared=SharedPaperStore(root/'shared.db');self.q=UnifiedQueue(root/'queue.db')
        self.addCleanup(self.shared.close);self.addCleanup(self.q.close)
        self.now=time.time();ms=int(self.now*1000)
        with self.shared.transaction():
            self.shared._set('spot','quote',{'price':'100','time':self.now})
            self.shared._set('futures','quote',{'price':'100','time':self.now})
        self.book=MarginPaperBook(self.shared.ledger);self.receiver=MarginCommandReceiver(self.book)
        filters={'lot_rules':[{'min':'0','max':'0','step':'.00000001'}],'min_notional':'0','max_notional':'0','observed_ms':ms}
        self.quotes={s:{'price':p,'time_ms':ms,'server_ms':ms,'source':'fixture','price_kind':'last_public_trade','filters':filters} for s,p in [('BTCUSDT','100'),('ETHUSDT','200')]}
        self.book.record_quotes(self.quotes,now_ms=ms)
        self.cross=CrossPaperBook(self.shared);self.cross_receiver=CrossCommandReceiver(self.cross)
        quotes={s:{'mark':'100','time':self.now,'funding_complete':True,'server_ms':ms,'next_due_ms':(ms//28800000+1)*28800000,'interval_ms':28800000} for s in ('BTCUSDT','ETHUSDT')}
        self.cross.record_quotes(quotes,now=self.now)

    def margin_commands(self,commands):
        self.q.receive(1,True,'/mode margin_cross',now=self.now)
        for n,command in enumerate(commands,2):self.q.receive(n,True,command,now=self.now)
        process_margin(self.q,self.receiver,self.book,self.shared,clock=lambda:self.now)
        return [r[0] for r in self.q.db.execute("SELECT result FROM unified_jobs WHERE market='margin_cross' ORDER BY id")]

    def test_no_loans_interest_and_detailed_status_are_distinct(self):
        loans,interest,status=self.margin_commands(['/loans','/interest','/status margin_cross'])
        self.assertIn('Активных займов нет',loans);self.assertNotIn('Входы на паузе',loans)
        self.assertIn('Ставки',interest);self.assertIn('Начислено',interest);self.assertIn('Следующего начисления нет',interest)
        for text in ('Собственный капитал','Свободно','Резерв ордеров','Позиции','Долг','Проценты','15m','2×','BTCUSDT','ETHUSDT','Ошибки'):
            self.assertIn(text,status)
        self.assertNotEqual(loans,interest);self.assertNotEqual(status,interest)
        self.assertIn('обеспечение позиции нельзя переводить',status.lower())
        out=[r[0] for r in self.q.db.execute('SELECT message FROM unified_outbox')]
        self.assertTrue(all(m.count('[Spot margin cross • PAPER]')<=1 for m in out))

    def test_open_loans_report_actual_asset_principal_interest_and_accrual(self):
        with self.shared.ledger.transaction():
            self.shared.ledger._post('fund','FIXTURE',{},[('margin_cross:free','USDT',Decimal(50)),('external:fixture','USDT',Decimal(-50))])
            self.shared.db.execute("UPDATE wallet_state SET paused=0 WHERE wallet='margin_cross'")
        self.book.open('long','p1','BTCUSDT','LONG','.09','5',2,self.quotes,now_ms=int(self.now*1000))
        self.book.open('short','p2','ETHUSDT','SHORT','.04','5',2,self.quotes,now_ms=int(self.now*1000))
        before=self.shared.db.execute('SELECT count(*) FROM ledger_events').fetchone()[0]
        loans,interest,status=self.margin_commands(['/loans','/interest','/status margin_cross'])
        for asset in ('USDT','ETH'):self.assertIn(asset,loans)
        self.assertIn('Основной долг',loans);self.assertIn('Последнее начисление',loans)
        self.assertIn('Следующее начисление',interest);self.assertIn('Начислено',interest)
        self.assertIn('BTCUSDT LONG',status);self.assertIn('ETHUSDT SHORT',status)
        self.assertNotIn('Активных займов нет',loans)
        self.assertEqual(before,self.shared.db.execute('SELECT count(*) FROM ledger_events').fetchone()[0])
        self.assertTrue(self.shared.ledger.reconcile()['balanced'])

    def test_general_status_uses_same_brief_shape_for_all_markets(self):
        self.q.receive(1,True,'/status',now=self.now)
        class Store:
            def get(inner,key):
                return {'paused':True,'quote':{'time':self.now},'position':None,'error':None,'blocked':None}.get(key)
        for market in ('spot','futures'):process_market(self.q,None,Store(),market)
        process_cross(self.q,self.cross_receiver,self.cross,now=self.now,clock=lambda:self.now)
        process_margin(self.q,self.receiver,self.book,self.shared,clock=lambda:self.now)
        rows=self.q.db.execute("SELECT market,result FROM unified_jobs WHERE command='/brief_status' ORDER BY market").fetchall()
        self.assertEqual(len(rows),4)
        for row in rows:
            for phrase in ('входы','позиций','обновление','состояние'):
                self.assertIn(phrase,row['result'].lower(),row['market'])
            self.assertLess(len(row['result']),250)

    def test_stale_prices_leave_exposed_equity_unknown_without_mutation(self):
        with self.shared.ledger.transaction():
            self.shared.ledger._post('fund','FIXTURE',{},[('margin_cross:free','USDT',Decimal(50)),('external:fixture','USDT',Decimal(-50))])
            self.shared.db.execute("UPDATE wallet_state SET paused=0 WHERE wallet='margin_cross'")
        self.book.open('long','p1','BTCUSDT','LONG','.09','5',2,self.quotes,now_ms=int(self.now*1000))
        count=self.shared.db.execute('SELECT count(*) FROM ledger_events').fetchone()[0]
        self.now+=121
        status=self.margin_commands(['/status margin_cross'])[0]
        self.assertIn('капитал после долгов: неизвестно',status.lower())
        self.assertIn('устарело',status);self.assertIn('оценка капитала неполная',status)
        self.assertEqual(count,self.shared.db.execute('SELECT count(*) FROM ledger_events').fetchone()[0])

    def test_mode_change_and_foreign_sender_do_not_reroute_accepted_report(self):
        self.q.receive(1,False,'/mode margin_cross',now=self.now)
        self.assertEqual(self.q.state('mode'),'spot')
        self.q.receive(2,True,'/mode margin_cross',now=self.now)
        self.q.receive(3,True,'/loans',now=self.now)
        self.q.receive(4,True,'/mode spot',now=self.now)
        process_margin(self.q,self.receiver,self.book,self.shared,clock=lambda:self.now)
        row=self.q.db.execute("SELECT market,result FROM unified_jobs WHERE update_id=3").fetchone()
        self.assertEqual(row['market'],'margin_cross');self.assertIn('Активных займов нет',row['result'])
        self.assertEqual(self.q.state('mode'),'spot')


if __name__=='__main__':unittest.main()
