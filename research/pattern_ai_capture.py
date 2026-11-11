"""One public USD-M candle capture, no credentials/retries/orders/daemon."""
import gzip,hashlib,json,time,urllib.parse,urllib.request,urllib.error
from pathlib import Path
import sys
HOST='https://fapi.binance.com'


def capture(symbol,*,derivatives=False):
    if symbol not in ('BTCUSDT','ETHUSDT','SOLUSDT'):raise ValueError('symbol_denied')
    from research.derivatives_context import PATHS
    receipts=[]
    def get(path,params=None):
        if path not in PATHS | {'/fapi/v1/time','/fapi/v1/klines'}:raise ValueError('public_endpoint_denied')
        url=HOST+path+('?' + urllib.parse.urlencode(params) if params else '')
        try:
            with urllib.request.urlopen(url,timeout=8 if path in PATHS else 20) as response:raw=response.read(2000000)
        except urllib.error.HTTPError as exc:
            status=exc.code;exc.close()
            raise ValueError('public_market_unavailable_http'+str(status)) from None
        receipts.append({'url':url,'sha256':hashlib.sha256(raw).hexdigest(),'received_ms':int(time.time()*1000)})
        return json.loads(raw)
    server=get('/fapi/v1/time')['serverTime'];now=int(time.time()*1000)
    if abs(server-now)>5000:raise ValueError('clock_skew')
    bar=900000;cursor=(server//86400000-32)*86400000;rows=[]
    for _ in range(4):
        data=get('/fapi/v1/klines',{'symbol':symbol,'interval':'15m','startTime':cursor,'endTime':server,'limit':1500})
        if not data:break
        for row in data:
            if row[6]!=row[0]+bar-1:raise ValueError('source_candle_time')
            if row[0]+bar<=server-60000:rows.append([int(row[0]),*map(float,row[1:6])])
        new=data[-1][0]+bar
        if new<=cursor:raise ValueError('source_pagination')
        cursor=new
        if cursor>server:break
    else:raise ValueError('capture_page_limit')
    result={'schema':1,'symbol':symbol,'venue':'binance-usdm','as_of_ms':int(time.time()*1000),'exchange_time_ms':server,'candles':rows,
            'source_receipts':receipts,'limitation':'Public closed candle snapshot, not proof of finality or executable price.'}
    if derivatives:
        from research.derivatives_context import collect
        signal=rows[-1][0]+bar
        result['derivatives']=collect(get,symbol=symbol,signal_ms=signal,now_ms=int(time.time()*1000),clock=lambda:int(time.time()*1000))
        result['as_of_ms']=int(time.time()*1000)
    return result

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('symbol');parser.add_argument('output');parser.add_argument('--derivatives-context',action='store_true');args=parser.parse_args()
    value=capture(args.symbol,derivatives=args.derivatives_context);path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    with gzip.open(path,'wt') as f:json.dump(value,f,separators=(',',':'))
    print(json.dumps({'symbol':value['symbol'],'closed_bars':len(value['candles']),'as_of_ms':value['as_of_ms'],'last_close_ms':value['candles'][-1][0]+900000}))
