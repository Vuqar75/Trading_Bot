import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from trading_bot.runtime_store import RuntimeStore
from trading_bot.app import Application, TelegramTransport
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.telegram_reports import status, signal_report


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'app.db'
        self.identity={'mode':'paper','interval':'15m','amount':'10'}
        self.store=RuntimeStore(self.path,self.identity)
        self.addCleanup(lambda:self.store.close())

    def reply(self, ident, command):
        self.store.command(ident,True,command)
        return self.store.db.execute('SELECT message FROM runtime_outbox ORDER BY id DESC LIMIT 1').fetchone()[0]

    def test_human_status(self):
        text=self.reply(1,'/status')
        for word in ['виртуальные','15m','на паузе','Позиция: нет','PnL','Блокировка: нет']:
            self.assertIn(word,text)
        self.assertNotIn('"position"',text)

    def test_signal_has_times_means_and_source(self):
        self.store.set('signal',dict(action='BUY',fast=101,slow=100,old_fast=99,old_slow=100,time=1,candle=0,interval='15m'))
        text=self.reply(1,'/signal')
        for word in ['Баку','SMA 10','SMA 30','пересечение вверх','УСТАРЕЛА','не подтверждение']:
            self.assertIn(word,text)

    def test_timeframe_requires_pause(self):
        self.reply(1,'/resume')
        self.assertIn('запрещена',self.reply(2,'/timeframe 5m'))
        self.assertIsNone(self.store.get('selected_interval'))

    def test_timeframe_persists_and_invalidates_signal(self):
        self.store.set('signal',{'action':'HOLD'})
        self.reply(1,'/timeframe 5m')
        self.assertIsNone(self.store.get('signal'))
        self.store.close()
        self.store=RuntimeStore(self.path,self.identity)
        app=Application(self.store,PaperExchange(),None)
        app.recover=lambda:None
        app.protective_close=lambda:True
        app.tick()
        self.assertEqual(app.interval,'5m')
        self.assertTrue(self.store.get('paused'))

    def test_position_and_pending_block_changes(self):
        p=self.store.get('position')
        self.store.set('position',{**p,'quantity':'0.1','cost':'10'})
        self.assertIn('запрещена',self.reply(1,'/timeframe 1h'))
        self.store.set('position',p)
        self.reply(2,'/resume')
        self.store.prepare('BUY','.1',Decimal(100),'pending','TEST')
        self.reply(3,'/pause')
        self.assertIn('запрещена',self.reply(4,'/timeframe 1h'))

    def test_invalid_value_and_duplicate(self):
        self.assertIn('Используйте',self.reply(1,'/timeframe 4h'))
        self.reply(2,'/timeframe 1h')
        self.reply(2,'/timeframe 1h')
        self.assertEqual(self.store.get('selected_interval'),'1h')
        self.assertIn('1h',self.reply(3,'/timeframe'))
