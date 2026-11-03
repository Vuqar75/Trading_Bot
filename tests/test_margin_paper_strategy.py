import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.margin_paper_strategy import MarginPaperStrategy
from trading_bot.paper_wallet_ledger import PaperWalletLedger
from trading_bot.margin_command_receiver import MarginCommandReceiver
from trading_bot.margin_app import cycle
from trading_bot.margin_spot_data import MarginSpotData
from trading_bot.unified_queue import UnifiedQueue


PERIOD=900000
SERVER=31*PERIOD+1000
FILTER={'lot_rules':[{'min':'.0001','max':'100','step':'.0001'}],
        'min_notional':'5','max_notional':'0','observed_ms':SERVER}


def snapshots(now=SERVER,btc='100'):
    return {'BTCUSDT':{'price':btc,'time_ms':now,'server_ms':now,'source':'fixture','price_kind':'last_public_trade','filters':FILTER},
            'ETHUSDT':{'price':'200','time_ms':now,'server_ms':now,'source':'fixture','price_kind':'last_public_trade','filters':FILTER}}


def candles():
    return {'BTCUSDT':[(i*PERIOD,'100') for i in range(30)]+[(30*PERIOD,'101')],
            'ETHUSDT':[(i*PERIOD,'100') for i in range(30)]+[(30*PERIOD,'99')]}


class MarginStrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ledger.db';self.ledger=PaperWalletLedger(self.path);self.addCleanup(self.ledger.close)
        self.book=MarginPaperBook(self.ledger)
        with self.ledger.transaction():
            self.ledger._post('seed','FIXTURE',{},[('margin_cross:free','USDT',Decimal(50)),('external:fixture','USDT',Decimal(-50))])
            self.ledger.db.execute("UPDATE wallet_state SET paused=0 WHERE wallet='margin_cross'")

    def test_candle_reference_before_boundary_survives_later_server_time(self):
        boundary=32*PERIOD;now=boundary+100;cutoff=boundary-100
        quotes=snapshots(now)
        for q in quotes.values():
            q['candle_server_ms']=cutoff
            q['filters']={**q['filters'],'observed_ms':now}
        strategy=MarginPaperStrategy(self.book)
        self.assertEqual(strategy.tick(quotes,candles(),now_ms=now),[])
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM margin_positions").fetchone()[0],0)
        self.assertEqual(strategy.last_protection_ms,now)
        self.assertTrue(self.ledger.reconcile()['balanced'])

    def test_invalid_or_stale_candle_reference_fails_closed(self):
        strategy=MarginPaperStrategy(self.book)
        for cutoff in (SERVER+1, SERVER-15001):
            quotes=snapshots()
            for q in quotes.values():q['candle_server_ms']=cutoff
            with self.subTest(cutoff=cutoff):
                with self.assertRaisesRegex(ValueError,'candle reference'):
                    strategy.tick(quotes,candles(),now_ms=SERVER)
        self.assertEqual(self.ledger.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0],1)
        self.assertTrue(self.ledger.reconcile()['balanced'])

    def test_boundary_snapshot_still_closes_position_for_protection(self):
        strategy=MarginPaperStrategy(self.book)
        self.assertEqual([x['action'] for x in strategy.tick(snapshots(),candles(),now_ms=SERVER)],['OPEN','OPEN'])
        boundary=32*PERIOD;now=boundary+100;quotes=snapshots(now,btc='95')
        for q in quotes.values():
            q['candle_server_ms']=boundary-100
            q['filters']={**q['filters'],'observed_ms':now}
        actions=strategy.tick(quotes,candles(),now_ms=now)
        self.assertEqual([x['action'] for x in actions],['STOP'])
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM margin_positions').fetchone()[0],1)
        self.assertEqual(strategy.last_protection_ms,now)
        self.assertTrue(self.ledger.reconcile()['balanced'])

    def test_candle_interval_rechecked_after_wait_for_financial_transaction(self):
        boundary=32*PERIOD;now=boundary-100;quotes=snapshots(now)
        for q in quotes.values():
            q['candle_server_ms']=now
            q['filters']={**q['filters'],'observed_ms':now}
        calls=[]
        def clock_ms():
            calls.append(1)
            return now if len(calls)<=2 else boundary+100
        strategy=MarginPaperStrategy(self.book)
        with self.assertRaisesRegex(ValueError,'candle interval expired'):
            strategy.tick(quotes,candles(),now_ms=now,clock_ms=clock_ms)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM margin_positions').fetchone()[0],0)
        self.assertTrue(self.ledger.reconcile()['balanced'])

    def test_two_signals_only_once_and_protective_exit_on_pause(self):
        strategy=MarginPaperStrategy(self.book)
        first=strategy.tick(snapshots(),candles(),now_ms=SERVER)
        self.assertEqual([x['action'] for x in first],['OPEN','OPEN'])
        self.assertEqual(strategy.tick(snapshots(),candles(),now_ms=SERVER),[])
        with self.ledger.transaction():self.ledger.db.execute("UPDATE wallet_state SET paused=1 WHERE wallet='margin_cross'")
        tick=strategy.tick(snapshots(SERVER+2000,btc='95'),candles(),now_ms=SERVER+2000)
        self.assertEqual([x['action'] for x in tick],['STOP'])
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM margin_positions').fetchone()[0],1)
        self.assertEqual(strategy.last_protection_ms,SERVER+2000)
        self.assertTrue(self.ledger.reconcile()['balanced'])

    def test_runtime_cycle_trades_only_after_trusted_snapshot_and_rejects_fixture_source(self):
        queue=UnifiedQueue(Path(self.tmp.name)/'commands.db');self.addCleanup(queue.close)
        receiver=MarginCommandReceiver(self.book);strategy=MarginPaperStrategy(self.book)
        class Reader:
            source=MarginSpotData.HOST
            def snapshot(self,symbol,interval):
                q=snapshots()[symbol].copy();q['source']=self.source
                q['filters']={**q['filters'],'notional_price':q['price'],'notional_time_ms':SERVER}
                q['candles']=candles()[symbol];return q
        reader=Reader()
        actions=cycle(None,self.book,queue,receiver,strategy,reader,started=0,clock=lambda:SERVER/1000)
        self.assertEqual([a['action'] for a in actions],['OPEN','OPEN'])
        reader.source='fixture'
        with self.assertRaisesRegex(ValueError,'Untrusted'):cycle(None,self.book,queue,receiver,strategy,reader,started=0,clock=lambda:SERVER/1000)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM ledger_events WHERE kind='MARGIN_TRADE'").fetchone()[0],2)

    def test_runtime_cycle_logs_only_allowlisted_diagnostic_fields(self):
        queue=UnifiedQueue(Path(self.tmp.name)/'commands.db');self.addCleanup(queue.close)
        receiver=MarginCommandReceiver(self.book);strategy=MarginPaperStrategy(self.book)
        class Reader:
            def snapshot(self,symbol,interval):raise ValueError('credential=secret https://example.com/?signature=secret')
        with self.assertLogs('margin-paper',level='ERROR') as output:
            with self.assertRaises(ValueError):cycle(None,self.book,queue,receiver,strategy,Reader(),started=0,clock=lambda:SERVER/1000)
        message=output.output[0];self.assertIn('"stage": "fetch"',message);self.assertIn('BTCUSDT',message)
        self.assertIn('"file": "margin_app.py"',message);self.assertRegex(message,r'"line": \d+')
        self.assertNotIn('secret',message);self.assertNotIn('https',message)

    def test_stale_data_blocks_entries_durably(self):
        strategy=MarginPaperStrategy(self.book)
        with self.assertRaises(ValueError):strategy.tick(snapshots(),candles(),now_ms=SERVER+200000)
        self.assertEqual(self.ledger.db.execute("SELECT paused FROM wallet_state WHERE wallet='margin_cross'").fetchone()[0],1)
        self.assertNotEqual(self.ledger.db.execute("SELECT value FROM margin_runtime WHERE key='error'").fetchone()[0],'null')

    def test_future_candle_is_not_used(self):
        strategy=MarginPaperStrategy(self.book);future=candles()
        future['BTCUSDT'][-1]=(31*PERIOD,'101')
        with self.assertRaises(ValueError):strategy.tick(snapshots(),future,now_ms=SERVER)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM margin_positions').fetchone()[0],0)

    def test_process_death_around_decision_commit(self):
        self.ledger.close()
        script='''import os,sqlite3,sys
from trading_bot.margin_paper_book import MarginPaperBook
from trading_bot.margin_paper_strategy import MarginPaperStrategy
from trading_bot.paper_wallet_ledger import PaperWalletLedger
db=sqlite3.connect(sys.argv[1],isolation_level=None);db.row_factory=sqlite3.Row
db.execute("PRAGMA foreign_keys=ON");db.execute("PRAGMA synchronous=FULL")
l=object.__new__(PaperWalletLedger);l.db=db;l.fault=lambda p:None
b=MarginPaperBook(l);p=900000;t=31*p+1000
f={"lot_rules":[{"min":".0001","max":"100","step":".0001"}],"min_notional":"5","max_notional":"0","observed_ms":t}
q={"BTCUSDT":{"price":"100","time_ms":t,"server_ms":t,"source":"fixture","price_kind":"last_public_trade","filters":f},"ETHUSDT":{"price":"200","time_ms":t,"server_ms":t,"source":"fixture","price_kind":"last_public_trade","filters":f}}
c={"BTCUSDT":[(i*p,"100") for i in range(30)]+[(30*p,"101")],"ETHUSDT":[(i*p,"100") for i in range(30)]+[(30*p,"99")]}
MarginPaperStrategy(b,fault=lambda point:os._exit(73) if point==sys.argv[2] else None).tick(q,c,now_ms=t)
'''
        for point,expected in [('after_decision_before_commit',0),('after_decision_commit',1)]:
            child=subprocess.run([sys.executable,'-B','-c',script,str(self.path),point],timeout=30)
            self.assertEqual(child.returncode,73)
            with sqlite3.connect(self.path) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM margin_positions').fetchone()[0],expected)
                self.assertEqual(db.execute('SELECT count(*) FROM margin_signals').fetchone()[0],expected)
        replay=subprocess.run([sys.executable,'-B','-c',script,str(self.path),'never'],timeout=30)
        self.assertEqual(replay.returncode,0)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM margin_positions').fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM ledger_events WHERE kind='MARGIN_TRADE'").fetchone()[0],2)


if __name__=='__main__':unittest.main()
