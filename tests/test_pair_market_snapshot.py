import time
"""Offline tests for symbol-bound public data; no exchange calls are made."""
import unittest
import sqlite3
from decimal import Decimal

from trading_bot.runtime_exchange import PaperExchange, market_snapshot
from trading_bot.testnet_observer import ObservationError
from trading_bot.runtime_store import RuntimeStore
from trading_bot.app import Application
from trading_bot.paper_pair_migration import tag_legacy_spot_orders
from trading_bot.telegram_reports import history, status
from unittest.mock import patch
from tests.test_paper_pair_catalog import spot_info


class FakeReader:
    def __init__(self, *, returned_symbol='SOLUSDT'):
        self.returned_symbol = returned_symbol
        self.calls = []
        self.server = 40 * 900_000

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == '/api/v3/time':
            return {'serverTime': self.server}
        if path == '/api/v3/ticker/price':
            return {'symbol': self.returned_symbol, 'price': '123.45'}
        if path == '/api/v3/klines':
            return [[n*900_000, 0, 0, 0, str(n+1), 0, (n+1)*900_000-1]
                    for n in range(40)] + [[self.server, 0, 0, 0, '999999', 0, self.server+899_999]]
        raise AssertionError(path)


class PairMarketSnapshotTests(unittest.TestCase):
    def test_experimental_sol_signal_then_protection_while_paused(self):
        import tempfile
        from pathlib import Path
        class Reader(FakeReader):
            def __init__(self):super().__init__();self.price='123.45'
            def get(self,path,params=None):
                if path=='/api/v3/exchangeInfo':
                    self.calls.append((path,params));return {'symbols':[spot_info()]}
                if path=='/api/v3/ticker/price':
                    self.calls.append((path,params));return {'symbol':'SOLUSDT','price':self.price}
                return super().get(path,params)
        with tempfile.TemporaryDirectory() as tmp:
            store=RuntimeStore(Path(tmp)/'spot.db',{'mode':'paper','symbol':'SOLUSDT'})
            reader=Reader()
            try:
                app=Application(store,PaperExchange(),reader,allow_pair_experiment=True)
                app.enable_paper_entries()
                with patch('trading_bot.app.signal_details',return_value={'action':'BUY'}):app.tick()
                self.assertGreater(Decimal(store.get('position')['quantity']),0)
                self.assertEqual(store.get('position')['symbol'],'SOLUSDT')
                with store.transaction():store.set('paused',True)
                reader.price='110'
                self.assertTrue(app.protective_close())
                self.assertEqual(Decimal(store.get('position')['quantity']),0)
                self.assertTrue(store.get('paused'))
                self.assertEqual(store.db.execute('SELECT count(*) FROM runtime_fills').fetchone()[0],2)
                self.assertTrue(all(p.get('symbol')=='SOLUSDT' for path,p in reader.calls if p and 'symbol' in p))
            finally:store.close()

    def test_experimental_pair_does_not_refresh_old_quote_after_slow_candles(self):
        import tempfile
        from pathlib import Path
        phase={'after_candles':False}
        class SlowReader(FakeReader):
            def get(self,path,params=None):
                if path=='/api/v3/exchangeInfo':return {'symbols':[spot_info()]}
                result=super().get(path,params)
                if path=='/api/v3/klines':phase['after_candles']=True
                return result
        with tempfile.TemporaryDirectory() as tmp:
            store=RuntimeStore(Path(tmp)/'spot.db',{'mode':'paper','symbol':'SOLUSDT'})
            try:
                app=Application(store,PaperExchange(),SlowReader(),allow_pair_experiment=True)
                app.enable_paper_entries()
                with patch('time.time',side_effect=lambda:1121 if phase['after_candles'] else 1000), patch('trading_bot.app.signal_details',return_value={'action':'BUY'}):
                    with self.assertRaisesRegex(ObservationError,'quote stale'):
                        app.tick()
                self.assertEqual(store.db.execute('SELECT count(*) FROM runtime_orders').fetchone()[0],0)
                self.assertEqual(store.get('quote')['time'],1000)
            finally:store.close()
    def test_stale_pair_quote_is_never_claimed(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            store=RuntimeStore(Path(tmp)/'spot.db',{'mode':'paper','symbol':'SOLUSDT'})
            try:
                with store.transaction():store.set('paused',False)
                with patch('trading_bot.runtime_store.time.time',return_value=1000):
                    with self.assertRaisesRegex(ObservationError,'stale'):
                        store.prepare('BUY',Decimal('.1'),Decimal('100'),'stale','fixture',quote_at=800)
                    client=store.prepare('BUY',Decimal('.1'),Decimal('100'),'fresh','fixture',quote_at=1000)
                with patch('trading_bot.runtime_store.time.time',return_value=1121):
                    self.assertIsNone(store.claim(client))
                self.assertEqual(store.db.execute('SELECT phase,status FROM runtime_orders WHERE client_id=?',(client,)).fetchone()[:],('SETTLED','EXPIRED'))
                self.assertEqual(store.db.execute('SELECT count(*) FROM runtime_fills').fetchone()[0],0)
            finally:store.close()

    def test_pair_quote_rechecked_after_actual_sqlite_wait(self):
        import concurrent.futures
        import tempfile
        import threading
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'spot.db';identity={'mode':'paper','symbol':'SOLUSDT'}
            store=RuntimeStore(path,identity)
            with store.transaction():store.set('paused',False)
            store.close();after=threading.Event()
            with sqlite3.connect(path,isolation_level=None) as lock:
                lock.execute('BEGIN IMMEDIATE')
                def worker():
                    s=RuntimeStore(path,identity)
                    try:return s.prepare('BUY',Decimal('.1'),Decimal('100'),'wait','fixture',quote_at=1000)
                    finally:s.close()
                with patch('time.time',side_effect=lambda:1121 if after.is_set() else 1000), concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future=pool.submit(worker);time.sleep(.15);self.assertFalse(future.done())
                    after.set();lock.commit()
                    with self.assertRaisesRegex(ObservationError,'quote stale'):
                        future.result(timeout=5)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM runtime_orders').fetchone()[0],0)
    def test_third_asset_uses_same_symbol_for_quote_and_candles(self):
        reader = FakeReader()
        price, candles, server = market_snapshot(reader, '15m', symbol='SOLUSDT')
        self.assertEqual(price, Decimal('123.45'))
        self.assertEqual(server, reader.server)
        self.assertEqual(len(candles), 40)
        self.assertNotIn(Decimal('999999'), [row[1] for row in candles])
        self.assertEqual(reader.calls[1][1]['symbol'], 'SOLUSDT')
        self.assertEqual(reader.calls[2][1]['symbol'], 'SOLUSDT')

    def test_wrong_returned_symbol_fails_before_candle_request(self):
        reader = FakeReader(returned_symbol='BTCUSDT')
        with self.assertRaisesRegex(ObservationError, 'symbol mismatch'):
            market_snapshot(reader, '15m', symbol='SOLUSDT')
        self.assertEqual(len(reader.calls), 2)

    def test_invalid_symbol_is_rejected_without_any_reader_call(self):
        for symbol in ('solusdt', 'USDTUSDT', 'BTCBUSD', '../BTCUSDT', '', None):
            with self.subTest(symbol=symbol):
                reader = FakeReader()
                with self.assertRaises(ObservationError):
                    market_snapshot(reader, '15m', symbol=symbol)
                self.assertEqual(reader.calls, [])

    def test_default_keeps_btc_compatibility(self):
        reader = FakeReader(returned_symbol='BTCUSDT')
        market_snapshot(reader, '15m')
        self.assertEqual(reader.calls[1][1]['symbol'], 'BTCUSDT')

    def test_paper_replay_keeps_saved_symbol_and_legacy_defaults(self):
        order={'client_id':'paper-order','side':'BUY','quantity':'0.25','price':'100','symbol':'SOLUSDT'}
        result=PaperExchange.inspect_saved(order)
        self.assertEqual(result['order']['symbol'],'SOLUSDT')
        self.assertEqual(result['fills'][0]['symbol'],'SOLUSDT')
        self.assertEqual(result,PaperExchange.inspect_saved(order))
        del order['symbol']
        legacy=PaperExchange.inspect_saved(order)
        self.assertEqual(legacy['order']['symbol'],'BTCUSDT')
        self.assertEqual(legacy['fills'][0]['symbol'],'BTCUSDT')
        db=sqlite3.connect(':memory:')
        db.row_factory=sqlite3.Row
        try:
            row=db.execute("SELECT 'old' AS client_id, 'BUY' AS side, '1' AS quantity, '100' AS price").fetchone()
            self.assertEqual(PaperExchange.inspect_saved(row)['order']['symbol'],'BTCUSDT')
        finally:
            db.close()

    def test_isolated_fixture_sol_fill_fee_close_and_replay(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'spot.db';identity={'mode':'paper','symbol':'SOLUSDT'}
            store=RuntimeStore(path,identity)
            try:
                with self.assertRaisesRegex(ObservationError,'not integrated'):
                    Application(store,PaperExchange(),FakeReader())
                with store.transaction():store.set('paused',False)
                client=store.prepare('BUY',Decimal('0.1'),Decimal('100'),'buy','fixture',quote_at=time.time())
                store.claim(client)
                order=store.db.execute('SELECT * FROM runtime_orders WHERE client_id=?',(client,)).fetchone()
                report=PaperExchange.inspect_saved({**dict(order),'symbol':'SOLUSDT'})
                report['fills'][0]['commissionAsset']='SOL';report['fills'][0]['commission']='0.001'
                import copy
                wrong=copy.deepcopy(report);wrong['order']['symbol']='BTCUSDT'
                with self.assertRaisesRegex(ObservationError,'identity'):
                    store.apply(client,wrong)
                self.assertEqual(Decimal(store.get('cash')),Decimal('1000'))
                store.apply(client,report);store.apply(client,report)
                pos=store.get('position')
                self.assertEqual(Decimal(pos['quantity']),Decimal('0.099'))
                self.assertEqual(Decimal(pos['cost']),Decimal('10'))
                sell=store.prepare('SELL',Decimal('0.099'),Decimal('110'),'sell','fixture',target=pos['id'],quote_at=time.time())
                store.claim(sell)
                row=store.db.execute('SELECT * FROM runtime_orders WHERE client_id=?',(sell,)).fetchone()
                closing=PaperExchange.inspect_saved({**dict(row),'symbol':'SOLUSDT'})
                store.apply(sell,closing);store.apply(sell,closing)
                self.assertEqual(Decimal(store.get('position')['quantity']),Decimal(0))
                self.assertEqual(Decimal(store.get('pnl')),Decimal('0.87911'))
            finally:store.close()
            store=RuntimeStore(path,identity)
            try:
                self.assertEqual(Decimal(store.get('pnl')),Decimal('0.87911'))
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM runtime_fills').fetchone()[0],2)
            finally:store.close()

    def test_history_keeps_each_orders_original_asset(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'spot.db';btc={'mode':'paper','symbol':'BTCUSDT'}
            store=RuntimeStore(path,btc)
            def trade(current,symbol,key):
                with current.transaction():current.set('paused',False)
                buy=current.prepare('BUY',Decimal('.1'),Decimal('100'),key+':buy','fixture',quote_at=time.time());current.claim(buy)
                row=current.db.execute('SELECT * FROM runtime_orders WHERE client_id=?',(buy,)).fetchone()
                current.apply(buy,PaperExchange.inspect_saved({**dict(row),'symbol':symbol}))
                pos=current.get('position')
                sell=current.prepare('SELL',Decimal('.1'),Decimal('110'),key+':sell','fixture',target=pos['id'],quote_at=time.time());current.claim(sell)
                row=current.db.execute('SELECT * FROM runtime_orders WHERE client_id=?',(sell,)).fetchone()
                current.apply(sell,PaperExchange.inspect_saved({**dict(row),'symbol':symbol}))
                with current.transaction():current.set('paused',True)
            try:trade(store,'BTCUSDT','btc')
            finally:store.close()
            tag_legacy_spot_orders(path)
            # Isolated fixture only: real pair migration has additional guards.
            store=RuntimeStore(path,btc)
            try:
                with store.transaction():store.set('identity',{'mode':'paper','symbol':'SOLUSDT'})
            finally:store.close()
            store=RuntimeStore(path,{'mode':'paper','symbol':'SOLUSDT'})
            try:
                trade(store,'SOLUSDT','sol')
                text=history(store)
                self.assertIn('Пара: BTCUSDT',text)
                self.assertIn('Пара: SOLUSDT',text)
                self.assertIn('Продано: 0.1 BTC',text)
                self.assertIn('Продано: 0.1 SOL',text)
                self.assertIn('Пара: SOLUSDT',status(store))
            finally:store.close()
            store=RuntimeStore(path,{'mode':'paper','symbol':'SOLUSDT'})
            try:
                with store.transaction():store.set('identity',btc)
            finally:store.close()
            store=RuntimeStore(path,btc)
            try:
                self.assertIn('Пара: BTCUSDT',history(store))
                self.assertIn('Пара: SOLUSDT',history(store))
            finally:store.close()
