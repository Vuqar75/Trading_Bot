import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from trading_bot.app import Application, TelegramTransport
from trading_bot.runtime_store import RuntimeStore
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.telegram_reports import status, history, daily_report
from trading_bot.user_text import error_text
from trading_bot.paper_monitor import snapshot, render_report, write_text
from trading_bot.futures_store import FuturesStore


class ReadableOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.path=self.root/'paper.db'
        self.s=RuntimeStore(self.path,{'mode':'paper'})
        self.addCleanup(self.s.close)
        self.app=Application(self.s,PaperExchange(),None)
        self.s.command(1,True,'/resume')

    def trade(self,key,price):
        self.app.execute('BUY','.1',Decimal(100),'buy'+key,'SIGNAL')
        p=self.s.get('position')
        self.app.execute('SELL','.1',Decimal(price),'sell'+key,'STOP',p['id'])

    def test_history_includes_actual_prices_fees_and_baku(self):
        self.trade('1',102)
        text=history(self.s)
        for value in ['Баку','100.00','102.00','+0.1798','Стоп-лосс','Комиссии продажи']:
            self.assertIn(value,text)
        self.assertNotIn('STOP',text)

    def test_block_and_error_translated_without_raw_dict(self):
        self.s.set('blocked','SUBMISSION_UNKNOWN')
        self.s.set('error',{'type':'ObservationError','reason':'Public futures data request failed'})
        text=status(self.s)
        self.assertIn('Неизвестен результат',text)
        self.assertIn('Не удалось получить',text)
        self.assertNotIn('SUBMISSION_UNKNOWN',text)
        self.assertNotIn('ObservationError',text)
        self.assertNotIn('SECRET',error_text({'reason':'SECRET arbitrary error'}))

    def test_daily_is_baku_not_utc_or_cumulative(self):
        self.trade('1',101)
        self.trade('2',102)
        now=datetime(2026,11,1,0,30,tzinfo=timezone.utc).timestamp()
        rows=self.s.db.execute('SELECT rowid FROM runtime_closes ORDER BY rowid').fetchall()
        # 23:59 and 00:01 Baku, both on Oct 31 in UTC.
        before=datetime(2026,10,31,19,59,tzinfo=timezone.utc).timestamp()
        after=datetime(2026,10,31,20,1,tzinfo=timezone.utc).timestamp()
        self.s.db.execute('UPDATE runtime_closes SET ts=? WHERE rowid=?',(before,rows[0][0]))
        self.s.db.execute('UPDATE runtime_closes SET ts=? WHERE rowid=?',(after,rows[1][0]))
        text=daily_report(self.s,now)
        self.assertIn('01.11.2026',text)
        self.assertIn('сегодня: +0.1798',text)
        self.s.set('quote',{'price':'102','time':now})
        report=snapshot(self.path,self.root/'obs.db',now)
        self.assertEqual(Decimal(report['daily_baku']['net_realized_pnl_usdt']),Decimal('.1798'))
        self.assertNotEqual(report['daily_baku']['net_realized_pnl_usdt'],report['net_realized_pnl_usdt'])
        self.assertIsNone(report['daily_baku']['fees_usdt'])

    def test_monitor_has_text_and_honest_missing_data(self):
        report=snapshot(self.path,self.root/'obs.db',1000)
        text=render_report(report)
        self.assertIn('ЗА СЕГОДНЯ ПО БАКУ',text)
        self.assertIn('ЗА ВСЁ НАБЛЮДЕНИЕ',text)
        self.assertIn('нет подтверждённых данных',text)
        target=self.root/'report.txt'
        write_text(target,text)
        self.assertEqual(target.read_text(),text+'\n')

    def test_long_reply_is_split_not_truncated(self):
        self.s.db.execute('DELETE FROM runtime_outbox')
        self.s.notify('A'*8500+'КОНЕЦ')
        sent=[]
        t=TelegramTransport('FAKE','1',lambda req:sent.append(json.loads(req.data)['text']) or {'ok':True,'result':{}})
        for _ in range(3):t.flush(self.s)
        self.assertEqual(len(sent),3)
        self.assertTrue(all(len(s)<=4096 for s in sent))
        self.assertTrue(sent[-1].endswith('КОНЕЦ'))
        self.assertFalse(self.s.db.execute('SELECT 1 FROM runtime_outbox').fetchone())

    def test_futures_error_and_signal_are_readable(self):
        f=FuturesStore(self.root/'futures.db')
        try:
            f.set('error',{'type':'ObservationError','reason':'Stale mark price'})
            f.set('signal',{'time':1,'action':'SELL','reason':'Входы на паузе.'})
            f.command(1,True,'/status')
            f.command(2,True,'/signal')
            text='\n'.join(r[0] for r in f.db.execute('SELECT message FROM runtime_outbox'))
            self.assertIn('данные устарели',text)
            self.assertIn('пересечение вниз',text)
            self.assertNotIn('ObservationError',text)
            self.assertNotIn('Сигнал: SELL',text)
        finally:f.close()

    def test_help_explains_commands(self):
        self.s.command(2,True,'/help')
        text=self.s.db.execute('SELECT message FROM runtime_outbox ORDER BY id DESC LIMIT 1').fetchone()[0]
        self.assertIn('/report — результат за сегодня',text)
        self.assertIn('программу не выключает',text)
