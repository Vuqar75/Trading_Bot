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
                client=store.prepare('BUY',Decimal('0.1'),Decimal('100'),'buy','fixture')
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
                sell=store.prepare('SELL',Decimal('0.099'),Decimal('110'),'sell','fixture',target=pos['id'])
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
                buy=current.prepare('BUY',Decimal('.1'),Decimal('100'),key+':buy','fixture');current.claim(buy)
                row=current.db.execute('SELECT * FROM runtime_orders WHERE client_id=?',(buy,)).fetchone()
                current.apply(buy,PaperExchange.inspect_saved({**dict(row),'symbol':symbol}))
                pos=current.get('position')
                sell=current.prepare('SELL',Decimal('.1'),Decimal('110'),key+':sell','fixture',target=pos['id']);current.claim(sell)
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
