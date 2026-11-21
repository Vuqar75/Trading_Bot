"""Fetch bounded public USD-M case data, save exact URL and digest before reuse."""
import json,hashlib,time
from pathlib import Path
import requests
from research.extreme_reversal import stamp
ROOT=Path('D:/TradingBot-ML-work/action-value-nov')
def get(name,endpoint,**params):
    p=ROOT/(name+'.json')
    meta=p.with_suffix('.meta')
    if not p.exists():
        response=requests.get('https://fapi.binance.com/fapi/v1/'+endpoint,params=dict(symbol='BTCUSDT',**params),timeout=25)
        response.raise_for_status()
        raw=response.content
        meta.write_text(json.dumps(dict(url=response.url,received=time.time(),sha256=hashlib.sha256(raw).hexdigest())))
        p.write_bytes(raw)
    assert hashlib.sha256(p.read_bytes()).hexdigest()==json.loads(meta.read_text())['sha256']
    return json.loads(p.read_bytes())
if __name__=='__main__':
    ROOT.mkdir(exist_ok=True)
    end=stamp('2026-11-21T04:45')
    start=stamp('2026-11-13')
    for a in range(start,end,86400000):
        b=min(end,a+86400000)-1
        for k in ('klines','markPriceKlines'):
            get(k+str(a),k,interval='1m',startTime=a,endTime=b,limit=1440)
    get('funding','fundingRate',startTime=start,endTime=end-1,limit=100)
    # Complete any four-hour exit following the observed crossing at 04:07 UTC.
    tail=stamp('2026-11-21T09:00')
    for k in ('klines','markPriceKlines'):
        get(k+str(end),k,interval='1m',startTime=end,endTime=tail-1,limit=500)
    get('funding2','fundingRate',startTime=end,endTime=tail-1,limit=10)
