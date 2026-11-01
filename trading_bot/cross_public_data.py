"""Public-only USD-M market data for the offline cross prototype.

Funding history by itself does not prove all expected payments were published;
the caller must establish coverage before allowing risk-increasing operations.
"""
from .cross_paper_book import SYMBOLS
from .futures_data import FuturesData, INTERVALS, signed_number
from .testnet_observer import ObservationError, number


class CrossPublicData(FuturesData):
    @staticmethod
    def symbol(symbol):
        if symbol not in SYMBOLS:
            raise ObservationError('Unsupported cross symbol')
        return symbol

    def mark(self, symbol):
        symbol=self.symbol(symbol)
        server=self.get('/fapi/v1/time')['serverTime']
        row=self.get('/fapi/v1/premiumIndex',{'symbol':symbol})
        if type(server) is not int or row.get('symbol')!=symbol or type(row.get('time')) is not int:
            raise ObservationError('Invalid mark identity/time')
        if not -5000 <= server-row['time'] <= 60000:
            raise ObservationError('Stale mark price')
        price=number(row['markPrice'])
        if price<=0:raise ObservationError('Zero mark price')
        return price,server

    def candles(self, symbol, interval, server):
        symbol=self.symbol(symbol)
        if interval not in INTERVALS:raise ObservationError('Invalid interval')
        seconds=INTERVALS[interval]
        rows=self.get('/fapi/v1/klines',{'symbol':symbol,'interval':interval,'limit':40})
        closed=[(int(r[0]),number(r[4])) for r in rows if int(r[6]) < server]
        if len(closed)<31 or any(p<=0 for _,p in closed):raise ObservationError('Insufficient valid futures candles')
        if any(b[0]-a[0]!=seconds*1000 for a,b in zip(closed,closed[1:])):
            raise ObservationError('Discontinuous futures candles')
        if not 0<=server-closed[-1][0]-seconds*1000<=seconds*1000:
            raise ObservationError('Stale futures candles')
        return closed

    def funding(self, symbol, start, end):
        symbol=self.symbol(symbol)
        if type(start) is not int or type(end) is not int or start<0 or end<0:
            raise ObservationError('Invalid funding range')
        if start>end:return []
        found={};cursor=start
        for _ in range(100):
            page=self.get('/fapi/v1/fundingRate',{'symbol':symbol,'startTime':cursor,'endTime':end,'limit':1000})
            if not isinstance(page,list) or len(page)>1000:raise ObservationError('Invalid funding page')
            previous=cursor-1;page_keys=set()
            for row in page:
                stamp=row.get('fundingTime')
                rate_type=row.get('rateType','Regular')
                key=(stamp,rate_type)
                if row.get('symbol')!=symbol or type(stamp) is not int or not previous<=stamp<=end or rate_type not in ('Regular','Special') or key in page_keys:
                    raise ObservationError('Funding page identity/order conflict')
                rate,mark=signed_number(row['fundingRate']),number(row['markPrice'])
                if mark<=0 or abs(rate)>=1:raise ObservationError('Invalid funding value')
                event={'time':stamp,'rate':str(rate),'mark':str(mark),'symbol':symbol,'rate_type':rate_type}
                if key in found and found[key]!=event:raise ObservationError('Conflicting funding page')
                found[key]=event;page_keys.add(key)
                previous=stamp
            if len(page)<1000:return [found[k] for k in sorted(found)]
            # Inclusive overlap is needed when two rate types share a timestamp.
            # A full page without time progress cannot establish completeness.
            if previous<=cursor:raise ObservationError('Ambiguous funding page boundary')
            cursor=previous
        raise ObservationError('Funding page limit reached')
