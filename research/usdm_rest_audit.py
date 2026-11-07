"""Preserve two public trade-kline endpoint snapshots at disputed boundaries."""
import datetime as dt,hashlib,json,sys,urllib.parse,urllib.request
from pathlib import Path
root=Path(sys.argv[1]);root.mkdir(parents=True,exist_ok=True);manifest=[]
for t in (1738114200000,1756449000000):
 for interval,limit in (('5m',3),('15m',1)):
  params=urllib.parse.urlencode({'symbol':'BTCUSDT','interval':interval,'startTime':t,'endTime':t+899999,'limit':limit})
  url='https://fapi.binance.com/fapi/v1/klines?'+params
  with urllib.request.urlopen(url,timeout=25) as response:raw=response.read()
  data=json.loads(raw)
  if len(data)!=limit or any(int(v[0])!=t+i*(300000 if interval=='5m' else 900000) for i,v in enumerate(data)):raise ValueError('incomplete REST audit')
  path=root/f'BTCUSDT-klines-{interval}-{t}.json';digest=hashlib.sha256(raw).hexdigest()
  if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('REST audit revision')
  path.write_bytes(raw);manifest.append({'url':url,'file':path.name,'sha256':digest,'received_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'rows':len(data),'ohlc':[r[1:5] for r in data]})
print(json.dumps(manifest,ensure_ascii=False,indent=2))
