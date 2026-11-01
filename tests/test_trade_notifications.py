import json
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from trading_bot.app import Application, TelegramTransport
from trading_bot.runtime_exchange import PaperExchange
from trading_bot.runtime_store import RuntimeStore
from trading_bot.futures_store import FuturesStore
from trading_bot.outbox_relay import FuturesOutboxRelay
from trading_bot.testnet_observer import ObservationError


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.s=RuntimeStore(self.root/'spot.db',{'mode':'paper','interval':'15m'})
        self.f=FuturesStore(self.root/'futures.db',slippage_bps='0')
        self.addCleanup(self.s.close)
        self.addCleanup(self.f.close)
        self.app=Application(self.s,PaperExchange(),None)
        self.s.command(1,True,'/resume')

    def messages(self):
        return [r[0] for r in self.s.db.execute('SELECT message FROM runtime_outbox ORDER BY id')]

    def test_spot_open_close_receipts(self):
        self.app.execute('BUY','.1',Decimal(100),'open','SIGNAL')
        pos=self.s.get('position')
        self.app.execute('SELL','.1',Decimal(102),'close','TAKE',pos['id'])
        entry,exit=self.messages()[-2:]
        for word in ['Spot','LONG','15m','100.00','0.01000000 USDT','Баку']:
            self.assertIn(word,entry)
        for word in ['закрыта','Цена входа: 100.00','102.00','+0.1798','Тейк-профит','0.01020000 USDT']:
            self.assertIn(word,exit)
        row=self.s.db.execute("SELECT * FROM runtime_orders WHERE side='SELL'").fetchone()
        self.s.apply(row['client_id'],PaperExchange.inspect_saved(row))
        self.assertEqual(len(self.messages()),3)

    def test_partial_order_net_cumulative(self):
        self.app.execute('BUY','.1',Decimal(100),'open','SIGNAL')
        pos=self.s.get('position')
        cid=self.s.prepare('SELL','.1',Decimal(102),'close','STOP',target=pos['id'])
        self.s.claim(cid)
        order={'clientOrderId':cid,'symbol':'BTCUSDT','orderId':1,'side':'SELL','status':'PARTIALLY_FILLED','origQty':'.1','executedQty':'.04','cummulativeQuoteQty':'4.08'}
        fill={'id':1,'orderId':1,'symbol':'BTCUSDT','isBuyer':False,'qty':'.04','quoteQty':'4.08','commission':'.00408','commissionAsset':'USDT'}
        self.s.apply(cid,{'order':order,'fills':[fill]})
        self.assertIn('частично закрыта',self.messages()[-1])
        order.update(status='FILLED',executedQty='.1',cummulativeQuoteQty='10.2')
        second={**fill,'id':2,'qty':'.06','quoteQty':'6.12','commission':'.00612'}
        self.s.apply(cid,{'order':order,'fills':[fill,second]})
        self.assertIn('+0.1798',self.messages()[-1])
        self.assertIn('0.01020000 USDT',self.messages()[-1])

    def test_legacy_entry_label_is_cost_not_fake_price(self):
        self.app.execute('BUY','.1',Decimal(100),'open','SIGNAL')
        pos=self.s.get('position')
        pos.pop('entry_price')
        self.s.set('position',pos)
        self.app.execute('SELL','.1',Decimal(102),'close','STOP',pos['id'])
        self.assertIn('Вход (себестоимость)',self.messages()[-1])

    def test_unvalued_fee_is_not_final_profit(self):
        cid=self.s.prepare('BUY','.1',Decimal(100),'open','SIGNAL')
        self.s.claim(cid)
        report=PaperExchange.inspect_saved(self.s.pending()[0])
        report['fills'][0].update(commissionAsset='BNB',commission='.0001')
        self.s.apply(cid,report)
        pos=self.s.get('position')
        self.app.execute('SELL','.1',Decimal(102),'close','STOP',pos['id'])
        self.assertIn('предварительный',self.messages()[-1])
        self.assertIn('BNB',self.messages()[-1])

    def populate_futures(self):
        self.f.command(1,True,'/resume')
        self.f.open_position('SHORT',Decimal(100),1000,'entry')
        pos=self.f.get('position')
        self.f.apply_funding(pos['id'],[],2000)
        self.f.close_position(Decimal(98),2000,'exit','TAKE',pos['id'])

    def test_futures_format_and_one_sender_relay(self):
        self.populate_futures()
        sent=[]
        def transport(req):
            sent.append(json.loads(req.data)['text'])
            return {'ok':True,'result':{}}
        t=TelegramTransport('FAKE','1',transport)
        relay=FuturesOutboxRelay(self.root/'futures.db')
        try:
            t.flush(relay)
            t.flush(relay)
            self.assertEqual(len(sent),3)
            self.assertIn('SHORT',sent[1])
            self.assertIn('Futures isolated',sent[1])
            self.assertIn('Итоговый PnL:',sent[2])
            self.assertIn('Funding:',sent[2])
            self.assertIn('Комиссии входа и выхода:',sent[2])
            self.assertEqual(self.f.get('offset'),2)
        finally:relay.close()

    def test_failed_delivery_preserves_future_outbox(self):
        self.populate_futures()
        before=self.f.db.execute('SELECT COUNT(*) FROM runtime_outbox').fetchone()[0]
        relay=FuturesOutboxRelay(self.root/'futures.db')
        try:
            t=TelegramTransport('FAKE','1',lambda req:(_ for _ in ()).throw(TimeoutError()))
            with self.assertRaises(TimeoutError):t.flush(relay)
            self.assertEqual(self.f.db.execute('SELECT COUNT(*) FROM runtime_outbox').fetchone()[0],before)
        finally:relay.close()

    def test_relay_excludes_second_sender_and_foreign_db(self):
        relay=FuturesOutboxRelay(self.root/'futures.db')
        try:
            with self.assertRaises(ObservationError):FuturesOutboxRelay(self.root/'futures.db')
        finally:relay.close()
        with self.assertRaises(Exception):FuturesOutboxRelay(self.root/'spot.db')
