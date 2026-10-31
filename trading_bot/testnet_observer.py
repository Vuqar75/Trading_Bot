"""Read-only Spot Testnet observer. No submit/cancel or Telegram methods.

Reports are observations, never an authorization to clear the offline ledger gate.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import tempfile
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ObservationError(RuntimeError):
    pass


def number(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ObservationError('Invalid numeric response') from None
    if not result.is_finite() or result < 0:
        raise ObservationError('Invalid numeric response')
    return result


class SpotTestnetReader:
    HOST = 'https://testnet.binance.vision'
    PATHS = {'/api/v3/time', '/api/v3/exchangeInfo', '/api/v3/ticker/price',
             '/api/v3/account', '/api/v3/order', '/api/v3/myTrades'}

    def __init__(self, key=None, secret=None, transport=None, clock=time.time):
        self.key, self.secret = key, secret
        self.transport = transport or self._http
        self.clock = clock

    @staticmethod
    def _http(req):
        try:
            with urlopen(req, timeout=20) as response:
                return json.load(response)
        except Exception:
            # URLs, headers and API error bodies must not reach logs.
            raise ObservationError('Testnet request failed') from None

    def get(self, path, params=None, signed=False):
        if path not in self.PATHS:
            raise ObservationError('Endpoint not allowed')
        params = dict(params or {})
        headers = {}
        if signed:
            if not self.key or not self.secret:
                raise ObservationError('Testnet credentials required')
            server = self.get('/api/v3/time')
            params.update(timestamp=int(server['serverTime']), recvWindow=5000)
            headers['X-MBX-APIKEY'] = self.key
        query = urlencode(params)
        if signed:
            query += '&signature=' + hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return self.transport(Request(self.HOST + path + ('?' + query if query else ''),
                                      headers=headers, method='GET'))

    @staticmethod
    def validate_symbol(symbol):
        if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9]{5,20}', symbol):
            raise ObservationError('Invalid symbol')

    def trades(self, symbol, order_id, max_pages=100):
        """Bounded pagination by trade ID, scoped to one immutable order ID.

        Exhausting pages is not a completeness proof: audit checks cumulative totals.
        """
        self.validate_symbol(symbol)
        cursor, found = None, {}
        for _ in range(max_pages):
            params = {'symbol': symbol, 'orderId': order_id, 'limit': 1000}
            if cursor is not None:
                params['fromId'] = cursor
            page = self.get('/api/v3/myTrades', params, signed=True)
            if not isinstance(page, list) or len(page) > 1000:
                raise ObservationError('Invalid trade page')
            ids = []
            for trade in page:
                ident = trade.get('id')
                if type(ident) is not int or ident < 0 or str(trade.get('orderId')) != str(order_id) or trade.get('symbol') != symbol:
                    raise ObservationError('Trade identity mismatch')
                if cursor is not None and ident < cursor:
                    raise ObservationError('Non-progressing trade page')
                if ident in found:
                    raise ObservationError('Duplicate trade identity')
                ids.append(ident)
                found[ident] = trade
            if ids != sorted(ids):
                raise ObservationError('Unordered trade page')
            if len(page) < 1000:
                return list(found.values())
            cursor = max(ids) + 1
        raise ObservationError('Trade page limit reached')

    def audit_order(self, symbol, client_id):
        self.validate_symbol(symbol)
        if not isinstance(client_id, str) or not re.fullmatch(r'[A-Za-z0-9._:/-]{1,36}', client_id):
            raise ObservationError('Invalid client order ID')
        params = {'symbol': symbol, 'origClientOrderId': client_id}
        first = self.get('/api/v3/order', params, signed=True)
        if first.get('symbol') != symbol or first.get('clientOrderId') != client_id:
            raise ObservationError('Order identity mismatch')
        fills = self.trades(symbol, first['orderId'])
        last = self.get('/api/v3/order', params, signed=True)
        fields = ('symbol', 'clientOrderId', 'orderId', 'side', 'status', 'origQty',
                  'executedQty', 'cummulativeQuoteQty', 'updateTime')
        if any(first.get(k) != last.get(k) for k in fields):
            raise ObservationError('Order changed during observation')
        if first.get('side') not in {'BUY', 'SELL'}:
            raise ObservationError('Invalid order side')
        quantity = sum((number(t['qty']) for t in fills), Decimal(0))
        quote = sum((number(t['quoteQty']) for t in fills), Decimal(0))
        if quantity != number(first['executedQty']) or quote != number(first['cummulativeQuoteQty']):
            raise ObservationError('Incomplete or inconsistent fills')
        original = number(first['origQty'])
        status = first['status']
        if status not in {'NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'PENDING_CANCEL', 'REJECTED', 'EXPIRED', 'EXPIRED_IN_MATCH'}:
            raise ObservationError('Unknown order status')
        if quantity > original or (status == 'FILLED' and quantity != original) or (status == 'PARTIALLY_FILLED' and not Decimal(0) < quantity < original) or (status in {'NEW', 'REJECTED'} and quantity != 0):
            raise ObservationError('Status and quantity disagree')
        fees = {}
        for trade in fills:
            if trade.get('isBuyer') is not (first['side'] == 'BUY'):
                raise ObservationError('Trade side mismatch')
            asset = trade['commissionAsset']
            fees[asset] = fees.get(asset, Decimal(0)) + number(trade['commission'])
        return {'order_id': str(first['orderId']), 'status': status,
                'executed_quantity': str(quantity), 'executed_quote': str(quote),
                'fees': {k: str(v) for k, v in fees.items()}, 'fill_count': len(fills),
                'gate_clear_authorized': False}

    def observe(self, symbol, client_ids=()):
        self.validate_symbol(symbol)
        rows = self.get('/api/v3/exchangeInfo', {'symbol': symbol})['symbols']
        if len(rows) != 1 or rows[0]['symbol'] != symbol or rows[0]['status'] != 'TRADING' or rows[0]['quoteAsset'] != 'USDT':
            raise ObservationError('Unsupported Spot symbol')
        price = number(self.get('/api/v3/ticker/price', {'symbol': symbol})['price'])
        if price == 0:
            raise ObservationError('Invalid price')
        audits = [self.audit_order(symbol, client) for client in client_ids]
        return {'observed_at': self.clock(), 'source': self.HOST, 'symbol': symbol,
                'price': str(price), 'orders': audits, 'mode': 'READ_ONLY',
                'gate_clear_authorized': False}


def write_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.observer-')
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbol', default='BTCUSDT')
    parser.add_argument('--client-id', action='append', default=[])
    parser.add_argument('--report', required=True)
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if args.interval < 5:
        parser.error('interval must be at least 5 seconds')
    reader = SpotTestnetReader(os.getenv('BINANCE_TESTNET_API_KEY'), os.getenv('BINANCE_TESTNET_API_SECRET'))
    while True:
        try:
            report = reader.observe(args.symbol, args.client_id)
        except Exception as exc:
            # A failed cycle replaces the prior successful report; no stale success.
            write_report(args.report, {'observed_at': time.time(), 'mode': 'READ_ONLY',
                                      'ok': False, 'error': type(exc).__name__,
                                      'gate_clear_authorized': False})
            if args.once:
                raise SystemExit(1) from None
        else:
            write_report(args.report, {**report, 'ok': True})
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
