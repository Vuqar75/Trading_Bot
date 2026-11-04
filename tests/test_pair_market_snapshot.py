"""Offline tests for symbol-bound public data; no exchange calls are made."""
import unittest
import sqlite3
from decimal import Decimal

from trading_bot.runtime_exchange import PaperExchange, market_snapshot
from trading_bot.testnet_observer import ObservationError


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
