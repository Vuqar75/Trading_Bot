"""Read-only public Binance Spot data for the paper margin experiment.

Only public GET endpoints are allowed. No keys, account, loans or order
endpoints are supported. A response is a paper-model input, not permission
to trade or a promise that a real order would pass all exchange checks.
"""
import json
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from .futures_data import INTERVALS
from .testnet_observer import ObservationError, number


class MarginSpotData:
    HOST='https://api.binance.com'
    PATHS={'/api/v3/time','/api/v3/trades','/api/v3/exchangeInfo','/api/v3/klines','/api/v3/referencePrice','/api/v3/avgPrice'}
    SYMBOLS={'BTCUSDT':'BTC','ETHUSDT':'ETH'}

    def __init__(self,transport=None,clock=time.time,*,experimental_pairs=None):
        from .paper_wallet_ledger import valid_asset
        extra=experimental_pairs or {}
        if (not isinstance(extra,dict) or len(extra)>100 or
            any(not isinstance(s,str) or not isinstance(a,str) or s!=a+'USDT' or
                not valid_asset(a) or a=='USDT' or s in self.SYMBOLS for s,a in extra.items())):
            raise ObservationError('Invalid experimental Spot pairs')
        self.symbols={**self.SYMBOLS,**extra}
        self.transport=transport or self._http;self.clock=clock

    @staticmethod
    def _http(request):
        try:
            with urlopen(request,timeout=15) as response:return json.load(response)
        except HTTPError as exc:
            try:code=json.load(exc).get('code')
            except Exception:code=None
            if code==-2043:return {'code':-2043}
            raise ObservationError('Public Spot market data unavailable') from None
        except Exception:
            raise ObservationError('Public Spot market data unavailable') from None

    def get(self,path,params=None):
        if path not in self.PATHS:raise ObservationError('Only public Spot data is allowed')
        return self.transport(Request(self.HOST+path+('?' + urlencode(params) if params else ''),method='GET'))

    def _symbol(self,symbol):
        if symbol not in self.symbols:raise ObservationError('Unsupported Spot symbol')
        return symbol

    def filters(self,symbol,server_ms):
        symbol=self._symbol(symbol);result=self.get('/api/v3/exchangeInfo',{'symbol':symbol})
        rows=result.get('symbols') if isinstance(result,dict) else None
        if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict):raise ObservationError('Invalid Spot symbol metadata')
        row=rows[0]
        if row.get('symbol')!=symbol or row.get('baseAsset')!=self.symbols[symbol] or row.get('quoteAsset')!='USDT' or row.get('status')!='TRADING' or 'MARKET' not in row.get('orderTypes',[]):
            raise ObservationError('Spot symbol is not eligible for paper pricing')
        if not isinstance(row.get('filters'),list):raise ObservationError('Missing Spot filters')
        by_type={}
        for item in row['filters']:
            if not isinstance(item,dict) or not isinstance(item.get('filterType'),str) or item['filterType'] in by_type:
                raise ObservationError('Duplicate or invalid Spot filter')
            by_type[item['filterType']]=item
        if 'LOT_SIZE' not in by_type:raise ObservationError('Spot lot filter missing')
        rules=[]
        for name in ('LOT_SIZE','MARKET_LOT_SIZE'):
            if name in by_type:
                f=by_type[name]
                try:rule={'min':str(number(f['minQty'])),'max':str(number(f['maxQty'])),'step':str(number(f['stepSize']))}
                except KeyError:raise ObservationError('Invalid Spot lot filter') from None
                rules.append(rule)
        floors=[];ceilings=[];averages=[]
        for name in ('MIN_NOTIONAL','NOTIONAL'):
            f=by_type.get(name)
            if f:
                try:
                    floors.append(number(f['minNotional']))
                    if name=='NOTIONAL':ceilings.append(number(f['maxNotional']))
                    if type(f.get('avgPriceMins')) is not int or f['avgPriceMins']<0:raise ValueError('Invalid average period')
                    averages.append(f['avgPriceMins'])
                except KeyError:raise ObservationError('Invalid Spot notional filter') from None
                except ValueError:raise ObservationError('Invalid Spot notional period') from None
        if not floors:raise ObservationError('Spot notional filter missing')
        positive=[v for v in ceilings if v>0]
        return {'lot_rules':rules,'min_notional':str(max(floors)),'max_notional':str(min(positive) if positive else 0),'observed_ms':server_ms,'notional_avg_minutes':averages}

    def snapshot(self,symbol,interval='15m'):
        symbol=self._symbol(symbol)
        if interval not in INTERVALS:raise ObservationError('Invalid Spot interval')
        before=self.get('/api/v3/time');start=before.get('serverTime') if isinstance(before,dict) else None
        if type(start) is not int or abs(int(self.clock()*1000)-start)>60000:raise ObservationError('Invalid Spot clock')
        filters=self.filters(symbol,start)
        trades=self.get('/api/v3/trades',{'symbol':symbol,'limit':1})
        if not isinstance(trades,list) or len(trades)!=1 or not isinstance(trades[0],dict):raise ObservationError('Invalid latest Spot trade')
        row=trades[0];trade_time=row.get('time')
        if type(trade_time) is not int or type(row.get('id')) is not int or row['id']<0:raise ObservationError('Invalid latest Spot trade identity')
        price=number(row.get('price'))
        if price<=0:raise ObservationError('Invalid Spot price')
        ref=self.get('/api/v3/referencePrice',{'symbol':symbol})
        if not isinstance(ref,dict):raise ObservationError('Invalid Spot reference price')
        if ref.get('referencePrice') is not None:
            if ref.get('symbol')!=symbol or type(ref.get('timestamp')) is not int:raise ObservationError('Invalid Spot reference identity')
            ref_price=number(ref['referencePrice']);ref_time=ref['timestamp'];ref_kind='exchange_reference'
        elif ref.get('code')==-2043 or (ref.get('symbol')==symbol and 'referencePrice' in ref and ref['referencePrice'] is None):
            periods=set(filters['notional_avg_minutes'])
            if periods=={0}:ref_price=price;ref_time=trade_time;ref_kind='last_public_trade'
            elif periods=={5}:
                avg=self.get('/api/v3/avgPrice',{'symbol':symbol})
                if not isinstance(avg,dict) or avg.get('mins')!=5 or type(avg.get('closeTime')) is not int:raise ObservationError('Spot average price unavailable')
                ref_price=number(avg.get('price'));ref_time=avg['closeTime'];ref_kind='five_minute_average'
            else:raise ObservationError('Unsupported Spot average price interval')
        else:raise ObservationError('Spot reference price unavailable')
        if ref_price<=0:raise ObservationError('Invalid Spot reference price')
        raw=self.get('/api/v3/klines',{'symbol':symbol,'interval':interval,'limit':40})
        after=self.get('/api/v3/time');end=after.get('serverTime') if isinstance(after,dict) else None
        if (type(end) is not int or not 0<=end-start<=15000 or abs(int(self.clock()*1000)-end)>60000 or
            not 0<=end-trade_time<=60000 or not 0<=end-ref_time<=60000):raise ObservationError('Stale Spot data request or price')
        filters.update(notional_price=str(ref_price),notional_time_ms=ref_time,notional_price_kind=ref_kind)
        if not isinstance(raw,list):raise ObservationError('Invalid Spot candles')
        seconds=INTERVALS[interval]*1000
        try:
            if any(not isinstance(r,list) or len(r)<7 or type(r[0]) is not int or type(r[6]) is not int or r[0]%seconds or r[6]!=r[0]+seconds-1 for r in raw):raise ValueError('Invalid candle boundary')
            closed=[(r[0],number(r[4])) for r in raw if r[6]<start]
        except (TypeError,ValueError,IndexError):raise ObservationError('Invalid Spot candles') from None
        if (len(closed)<31 or any(v<=0 for _,v in closed) or any(b[0]-a[0]!=seconds for a,b in zip(closed,closed[1:])) or
            not 0<=start-closed[-1][0]-seconds<=seconds):raise ObservationError('Stale or discontinuous Spot candles')
        return {'symbol':symbol,'source':self.HOST,'price_kind':'last_public_trade','time_ms':trade_time,'price':str(price),'filters':filters,'candles':closed,'server_ms':end,'candle_server_ms':start}
