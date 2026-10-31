"""Public USD-M perpetual data only. No account, signing or order endpoint."""
import json
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from .testnet_observer import ObservationError, number

INTERVALS = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600}


def signed_number(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ObservationError('Invalid funding rate') from None
    if not result.is_finite():
        raise ObservationError('Invalid funding rate')
    return result


class FuturesData:
    HOST = 'https://fapi.binance.com'
    PATHS = {'/fapi/v1/time', '/fapi/v1/premiumIndex', '/fapi/v1/klines', '/fapi/v1/fundingRate'}

    def __init__(self, transport=None):
        self.transport = transport or self._http

    @staticmethod
    def _http(request):
        try:
            with urlopen(request, timeout=15) as response:
                return json.load(response)
        except Exception:
            raise ObservationError('Public futures data request failed') from None

    def get(self, path, params=None):
        if path not in self.PATHS:
            raise ObservationError('Only public futures market data is allowed')
        return self.transport(Request(self.HOST + path + ('?' + urlencode(params) if params else ''), method='GET'))

    def mark(self):
        server = self.get('/fapi/v1/time')['serverTime']
        row = self.get('/fapi/v1/premiumIndex', {'symbol': 'BTCUSDT'})
        if type(server) is not int or row.get('symbol') != 'BTCUSDT' or type(row.get('time')) is not int:
            raise ObservationError('Invalid mark identity/time')
        if not -5000 <= server - row['time'] <= 60000:
            raise ObservationError('Stale mark price')
        price = number(row['markPrice'])
        if not price:
            raise ObservationError('Zero mark price')
        return price, server

    def candles(self, interval, server):
        seconds = INTERVALS[interval]
        rows = self.get('/fapi/v1/klines', {'symbol': 'BTCUSDT', 'interval': interval, 'limit': 40})
        closed = [(int(r[0]), number(r[4])) for r in rows if int(r[6]) < server]
        if len(closed) < 31 or any(p <= 0 for _, p in closed):
            raise ObservationError('Insufficient valid futures candles')
        if any(b[0] - a[0] != seconds*1000 for a, b in zip(closed, closed[1:])):
            raise ObservationError('Discontinuous futures candles')
        if not 0 <= server - closed[-1][0] - seconds*1000 <= seconds*1000:
            raise ObservationError('Stale futures candles')
        return closed

    def funding(self, start, end):
        if start > end:
            return []
        found = []
        cursor = start
        for _ in range(100):
            page = self.get('/fapi/v1/fundingRate', {'symbol': 'BTCUSDT', 'startTime': cursor, 'endTime': end, 'limit': 1000})
            if not isinstance(page, list) or len(page) > 1000:
                raise ObservationError('Invalid funding page')
            previous = cursor - 1
            for row in page:
                stamp = row.get('fundingTime')
                if row.get('symbol') != 'BTCUSDT' or type(stamp) is not int or not previous < stamp <= end:
                    raise ObservationError('Funding page identity/order conflict')
                rate, mark = signed_number(row['fundingRate']), number(row['markPrice'])
                if not mark or abs(rate) >= 1:
                    raise ObservationError('Invalid funding value')
                found.append({'time': stamp, 'rate': str(rate), 'mark': str(mark)})
                previous = stamp
            if len(page) < 1000:
                return found
            cursor = previous + 1
            if cursor > end:
                return found
        raise ObservationError('Funding page limit reached')
