import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from trading_bot.testnet_observer import SpotTestnetReader, ObservationError, write_report


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.order = dict(symbol='BTCUSDT', clientOrderId='tb1', orderId=7,
                          side='BUY', status='FILLED', origQty='0.001',
                          executedQty='0.001', cummulativeQuoteQty='70', updateTime=1)
        self.fill = dict(symbol='BTCUSDT', orderId=7, id=10, qty='0.001',
                         quoteQty='70', commission='0.000001', commissionAsset='BTC', isBuyer=True)
        self.calls = []
        def transport(req):
            self.calls.append(req)
            path = urlparse(req.full_url).path
            return copy.deepcopy({'/api/v3/time': {'serverTime': 1000},
                '/api/v3/order': self.order, '/api/v3/myTrades': [self.fill]}[path])
        self.reader = SpotTestnetReader('fake-key', 'fake-secret', transport)

    def test_read_only_signed_audit_preserves_fees(self):
        report = self.reader.audit_order('BTCUSDT', 'tb1')
        self.assertEqual(report['fees'], {'BTC': '0.000001'})
        self.assertFalse(report['gate_clear_authorized'])
        self.assertTrue(all(r.get_method() == 'GET' for r in self.calls))
        signed = [r for r in self.calls if urlparse(r.full_url).path != '/api/v3/time']
        self.assertTrue(all('signature' in parse_qs(urlparse(r.full_url).query) for r in signed))

    def test_missing_fills_block(self):
        self.order['executedQty'] = '0.002'
        with self.assertRaises(ObservationError): self.reader.audit_order('BTCUSDT', 'tb1')

    def test_partial_with_full_quantity_blocks(self):
        self.order['status'] = 'PARTIALLY_FILLED'
        with self.assertRaises(ObservationError): self.reader.audit_order('BTCUSDT', 'tb1')

    def test_changed_order_blocks(self):
        original = self.reader.transport
        count = 0
        def changing(req):
            nonlocal count
            result = original(req)
            if urlparse(req.full_url).path == '/api/v3/order':
                count += 1
                if count == 2: result['updateTime'] = 2
            return result
        self.reader.transport = changing
        with self.assertRaises(ObservationError): self.reader.audit_order('BTCUSDT', 'tb1')

    def test_duplicate_and_wrong_side_block(self):
        self.fill['isBuyer'] = False
        with self.assertRaises(ObservationError): self.reader.audit_order('BTCUSDT', 'tb1')
        self.reader.transport = lambda req: [self.fill, self.fill] if urlparse(req.full_url).path.endswith('myTrades') else {'serverTime': 1}
        with self.assertRaises(ObservationError): self.reader.trades('BTCUSDT', 7)

    def test_pagination_and_limit(self):
        calls = []
        def transport(req):
            query = parse_qs(urlparse(req.full_url).query)
            if urlparse(req.full_url).path.endswith('/time'): return {'serverTime': 1}
            calls.append(query)
            if 'fromId' in query: return []
            return [{**self.fill, 'id': i} for i in range(1000)]
        self.reader.transport = transport
        self.assertEqual(len(self.reader.trades('BTCUSDT', 7)), 1000)
        self.assertEqual(calls[-1]['fromId'], ['1000'])
        with self.assertRaises(ObservationError): self.reader.trades('BTCUSDT', 7, max_pages=1)

    def test_no_credentials_and_no_mutating_endpoint(self):
        with self.assertRaises(ObservationError): SpotTestnetReader().get('/api/v3/account', signed=True)
        with self.assertRaises(ObservationError): self.reader.get('/api/v3/openOrders')
        with self.assertRaises(ObservationError): self.reader.audit_order('BTCUSDT?x=1', 'tb1')

    def test_report_replacement_and_transport_redaction(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'status.json'
            write_report(path, {'ok': True})
            write_report(path, {'ok': False})
            self.assertIn('false', path.read_text())
        with patch('trading_bot.testnet_observer.urlopen', side_effect=RuntimeError('SECRET')):
            with self.assertRaises(ObservationError) as error:
                SpotTestnetReader().get('/api/v3/time')
        self.assertNotIn('SECRET', str(error.exception))
