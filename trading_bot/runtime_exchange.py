"""Narrow BTCUSDT Spot Testnet execution adapter; production host is absent."""
import hashlib
import hmac
import time
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode
from urllib.request import Request
from .testnet_observer import SpotTestnetReader, ObservationError, number


class TestnetExchange(SpotTestnetReader):
    mode = 'testnet'

    def __init__(self, key, secret, *, enabled=False, transport=None, clock=time.time):
        if not key or not secret:
            raise ObservationError('Dedicated Spot Testnet credentials required')
        super().__init__(key, secret, transport, clock)
        self.enabled = enabled

    def submit(self, client_id, side, quantity):
        if not self.enabled:
            raise ObservationError('Testnet sends disabled')
        if side not in {'BUY', 'SELL'} or number(quantity) <= 0:
            raise ObservationError('Invalid order')
        params = {'symbol': 'BTCUSDT', 'side': side, 'type': 'MARKET',
                  'quantity': quantity, 'newClientOrderId': client_id,
                  'newOrderRespType': 'ACK',
                  'timestamp': int(self.get('/api/v3/time')['serverTime']), 'recvWindow': 5000}
        query = urlencode(params)
        query += '&signature=' + hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        # Exactly one attempt. Every exception is ambiguous to the caller.
        return self.transport(Request(self.HOST + '/api/v3/order', data=query.encode(),
            headers={'X-MBX-APIKEY': self.key, 'Content-Type': 'application/x-www-form-urlencoded'}, method='POST'))

    def inspect(self, client_id):
        return self.audit_order('BTCUSDT', client_id, details=True)

    def balances(self):
        account = self.get('/api/v3/account', signed=True)
        if account.get('canTrade') is not True:
            raise ObservationError('Account trading disabled')
        return {r['asset']: number(r['free']) for r in account['balances']}

    def open_orders(self):
        result = self.get('/api/v3/openOrders', {'symbol': 'BTCUSDT'}, signed=True)
        if not isinstance(result, list):
            raise ObservationError('Invalid open orders')
        return result


def market_snapshot(reader, interval):
    seconds = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600}[interval]
    server = int(reader.get('/api/v3/time')['serverTime'])
    quote = reader.get('/api/v3/ticker/price', {'symbol': 'BTCUSDT'})
    if quote.get('symbol') != 'BTCUSDT':
        raise ObservationError('Quote symbol mismatch')
    price = number(quote['price'])
    if not price:
        raise ObservationError('Zero quote')
    rows = reader.get('/api/v3/klines', {'symbol': 'BTCUSDT', 'interval': interval, 'limit': 40})
    closed = [(int(r[0]), number(r[4])) for r in rows if int(r[6]) < server]
    if len(closed) < 31 or any(b[0] - a[0] != seconds * 1000 for a, b in zip(closed, closed[1:])):
        raise ObservationError('Insufficient or discontinuous candles')
    if not 0 <= server - closed[-1][0] - seconds * 1000 <= seconds * 1000:
        raise ObservationError('Stale candles')
    return price, closed, server


def order_quantity(info, desired, price):
    if info.get('symbol') != 'BTCUSDT' or info.get('status') != 'TRADING' or info.get('quoteAsset') != 'USDT' or info.get('baseAsset') != 'BTC':
        raise ObservationError('Unsupported symbol')
    if 'MARKET' not in info.get('orderTypes', []):
        raise ObservationError('Market orders unavailable')
    filters = {f['filterType']: f for f in info['filters']}
    lot = filters.get('LOT_SIZE')
    if lot is None:
        raise ObservationError('Missing quantity filter')
    quantity = number(desired)
    for name in ('LOT_SIZE', 'MARKET_LOT_SIZE'):
        rule = filters.get(name)
        if rule:
            step = number(rule['stepSize'])
            if step:
                quantity = (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step
    for name in ('LOT_SIZE', 'MARKET_LOT_SIZE'):
        rule = filters.get(name)
        if rule:
            step, minimum, maximum = (number(rule[k]) for k in ('stepSize', 'minQty', 'maxQty'))
            if (step and quantity % step) or quantity < minimum or (maximum and quantity > maximum):
                raise ObservationError('Quantity outside exchange filters')
    notional = quantity * price
    for name in ('MIN_NOTIONAL', 'NOTIONAL'):
        rule = filters.get(name)
        if rule:
            # Apply conservatively even when the API marks a filter optional.
            if notional < number(rule['minNotional']):
                raise ObservationError('Notional below exchange minimum')
            if name == 'NOTIONAL' and number(rule['maxNotional']) and notional > number(rule['maxNotional']):
                raise ObservationError('Notional above exchange maximum')
    if quantity <= 0:
        raise ObservationError('Untradeable residual (dust)')
    return format(quantity, 'f')


class PaperExchange:
    """Deterministic durable fill reconstructed by client ID; no external sends."""
    mode = 'paper'

    def submit(self, client_id, side, quantity):
        return {'clientOrderId': client_id}

    @staticmethod
    def inspect_saved(order):
        qty, price = number(order['quantity']), number(order['price'])
        quote = qty * price
        return {'order': {'clientOrderId': order['client_id'], 'symbol': 'BTCUSDT',
                    'orderId': order['client_id'], 'side': order['side'], 'status': 'FILLED',
                    'origQty': str(qty), 'executedQty': str(qty), 'cummulativeQuoteQty': str(quote)},
                'fills': [{'id': order['client_id'], 'orderId': order['client_id'],
                    'symbol': 'BTCUSDT', 'isBuyer': order['side'] == 'BUY', 'qty': str(qty),
                    'quoteQty': str(quote), 'commission': str(quote * Decimal('0.001')),
                    'commissionAsset': 'USDT'}]}
