"""Synthetic USD-M symbol and filter checks; never calls a network endpoint."""
import copy
import unittest
import tempfile
import time
import sqlite3
import concurrent.futures
import threading
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from tests.test_paper_pair_catalog import spot_info
from trading_bot.futures_data import FuturesData
from trading_bot.paper_pair_catalog import PairMetadataError, usd_m_perpetual_candidate
from trading_bot.testnet_observer import ObservationError
from trading_bot.futures_store import FuturesStore
from trading_bot.futures_app import FuturesApplication


class PairFuturesDataTests(unittest.TestCase):
    def metadata(self):
        data=spot_info();data.update(contractType='PERPETUAL',marginAsset='USDT')
        data['filters'][-1]={'filterType':'MIN_NOTIONAL','notional':'5'}
        return data

    def test_contract_preview_rejects_non_perpetual_or_wrong_margin(self):
        data=self.metadata();original=copy.deepcopy(data)
        self.assertEqual(usd_m_perpetual_candidate(data,'SOLUSDT','.1234','100')['quantity'],'0.123')
        self.assertEqual(data,original)
        for change in ({'contractType':'CURRENT_QUARTER'},{'marginAsset':'BTC'},{'status':'BREAK'}):
            bad=copy.deepcopy(data);bad.update(change)
            with self.subTest(change=change),self.assertRaises(PairMetadataError):
                usd_m_perpetual_candidate(bad,'SOLUSDT','.1','100')

    def test_futures_reader_binds_mark_candles_and_funding_to_symbol(self):
        calls=[];server=40*900000
        def transport(request):
            url=urlparse(request.full_url);params=parse_qs(url.query);calls.append((url.path,params))
            if url.path.endswith('/time'):return {'serverTime':server}
            if url.path.endswith('/premiumIndex'):return {'symbol':'SOLUSDT','time':server,'markPrice':'100'}
            if url.path.endswith('/klines'):
                return [[n*900000,0,0,0,'100',0,(n+1)*900000-1] for n in range(40)]
            if url.path.endswith('/fundingRate'):
                return [{'symbol':'SOLUSDT','fundingTime':1000,'fundingRate':'0.001','markPrice':'100'}]
            raise AssertionError(url.path)
        reader=FuturesData(transport,symbol='SOLUSDT')
        mark,stamp=reader.mark();self.assertEqual(str(mark),'100')
        self.assertEqual(len(reader.candles('15m',stamp)),40)
        self.assertEqual(reader.funding(0,2000)[0]['time'],1000)
        self.assertTrue(all(params.get('symbol')==['SOLUSDT'] for path,params in calls if not path.endswith('/time')))

    def test_wrong_symbol_and_invalid_identifier_fail_closed(self):
        for symbol in ('../SOLUSDT','USDTUSDT','solusdt','BTCBUSD'):
            with self.subTest(symbol=symbol),self.assertRaises(ObservationError):FuturesData(symbol=symbol)
        reader=FuturesData(lambda request: {'serverTime':1000} if request.full_url.endswith('/time') else
                           {'symbol':'BTCUSDT','time':1000,'markPrice':'100'},symbol='SOLUSDT')
        with self.assertRaisesRegex(ObservationError,'identity'):reader.mark()
        wrong=FuturesData(lambda request:[{'symbol':'BTCUSDT','fundingTime':1000,'fundingRate':'0.001','markPrice':'100'}],symbol='SOLUSDT')
        with self.assertRaisesRegex(ObservationError,'identity'):wrong.funding(0,2000)

    def test_isolated_fixture_sol_long_short_funding_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'future.db'
            store=FuturesStore(path,fee_bps='0',slippage_bps='0',symbol='SOLUSDT',allow_pair_experiment=True)
            try:
                with self.assertRaisesRegex(ObservationError,'not integrated'):
                    FuturesApplication(store,FuturesData(symbol='SOLUSDT'))
                store.command(1,True,'/resume')
                for index,(direction,exit_price,expected) in enumerate((('LONG','110',Decimal('.99')),('SHORT','90',Decimal('1.01')))):
                    start=1000+index*3000
                    self.assertTrue(store.open_position(direction,Decimal('100'),start,'open:'+str(index),
                        metadata=self.metadata(),quote_symbol='SOLUSDT',observed_at=time.time()))
                    pos=store.get('position');self.assertEqual(pos['symbol'],'SOLUSDT')
                    self.assertEqual(Decimal(pos['quantity']),Decimal('.1'))
                    bad={'symbol':'BTCUSDT','time':start+1000,'rate':'.001','mark':'100'}
                    with self.assertRaisesRegex(ObservationError,'symbol'):
                        store.apply_funding(pos['id'],[bad],start+1000)
                    event={**bad,'symbol':'SOLUSDT'}
                    store.apply_funding(pos['id'],[event],start+1000)
                    store.apply_funding(pos['id'],[event],start+1000)
                    with self.assertRaisesRegex(ObservationError,'identity or freshness'):
                        store.close_position(Decimal(exit_price),start+1000,'close:'+str(index),'fixture',pos['id'],quote_symbol='BTCUSDT',observed_at=time.time())
                    self.assertTrue(store.close_position(Decimal(exit_price),start+1000,'close:'+str(index),'fixture',pos['id'],quote_symbol='SOLUSDT',observed_at=time.time()))
                    self.assertEqual(Decimal(store.get('pnl')),expected if index==0 else Decimal('2'))
                self.assertEqual(store.db.execute('SELECT count(*) FROM futures_funding').fetchone()[0],2)
            finally:store.close()
            store=FuturesStore(path,fee_bps='0',slippage_bps='0',symbol='SOLUSDT',allow_pair_experiment=True)
            try:self.assertEqual(Decimal(store.get('cash')),Decimal('1002'))
            finally:store.close()

    def test_sol_quantity_filter_and_used_margin_are_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=FuturesStore(Path(tmp)/'future.db',fee_bps='0',slippage_bps='0',symbol='SOLUSDT',allow_pair_experiment=True)
            try:
                store.command(1,True,'/resume');info=self.metadata()
                info['filters'][0].update(minQty='.03',stepSize='.03')
                with patch('trading_bot.futures_store.time.time',return_value=1000):
                    with self.assertRaisesRegex(ObservationError,'freshness'):
                        store.open_position('LONG',Decimal('100'),1000,'stale',metadata=info,quote_symbol='SOLUSDT',observed_at=879)
                    self.assertTrue(store.open_position('LONG',Decimal('100'),1000,'fresh',metadata=info,quote_symbol='SOLUSDT',observed_at=1000))
                pos=store.get('position')
                self.assertEqual(Decimal(pos['quantity']),Decimal('.09'))
                self.assertEqual(Decimal(pos['margin']),Decimal('9'))
                self.assertEqual(Decimal(store.get('cash')),Decimal('991'))
            finally:store.close()

    def test_sol_quote_expires_while_waiting_for_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'future.db'
            store=FuturesStore(path,fee_bps='0',slippage_bps='0',symbol='SOLUSDT',allow_pair_experiment=True)
            store.command(1,True,'/resume');store.close();after=threading.Event()
            def worker():
                s=FuturesStore(path,fee_bps='0',slippage_bps='0',symbol='SOLUSDT',allow_pair_experiment=True)
                try:return s.open_position('LONG',Decimal('100'),1000,'wait',metadata=self.metadata(),quote_symbol='SOLUSDT',observed_at=1000)
                finally:s.close()
            with sqlite3.connect(path,isolation_level=None) as lock:
                lock.execute('BEGIN IMMEDIATE')
                with patch('time.time',side_effect=lambda:1121 if after.is_set() else 1000), concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future=pool.submit(worker);time.sleep(.15);self.assertFalse(future.done());after.set();lock.commit()
                    with self.assertRaisesRegex(ObservationError,'freshness'):future.result(timeout=5)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM futures_events WHERE event='OPEN'").fetchone()[0],0)

    def test_experimental_isolated_application_sol_protects_while_paused(self):
        class Reader:
            symbol='SOLUSDT'
            def __init__(self,metadata):self.metadata=metadata;self.price=Decimal('100');self.server=1000
            def mark(self):return self.price,self.server
            def candles(self,interval,server):return [(n*900000,Decimal('100')) for n in range(40)]
            def funding(self,start,end):return []
            def get(self,path):
                if path!='/fapi/v1/exchangeInfo':raise AssertionError(path)
                return {'symbols':[self.metadata]}
        with tempfile.TemporaryDirectory() as tmp:
            store=FuturesStore(Path(tmp)/'future.db',symbol='SOLUSDT',allow_pair_experiment=True)
            reader=Reader(self.metadata())
            try:
                app=FuturesApplication(store,reader,allow_pair_experiment=True)
                store.command(1,True,'/resume')
                with patch('trading_bot.futures_app.signal_details',return_value={'action':'BUY'}):app.tick()
                self.assertEqual(store.get('position')['symbol'],'SOLUSDT')
                self.assertIn('SOLUSDT',store.status_text())
                store.command(2,True,'/pause');reader.price=Decimal('95');reader.server=2000
                app.tick()
                self.assertIsNone(store.get('position'))
                self.assertTrue(store.get('paused'))
                self.assertEqual(store.db.execute("SELECT count(*) FROM futures_events WHERE event='CLOSE'").fetchone()[0],1)
            finally:store.close()
