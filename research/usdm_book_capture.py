"""Bounded public USD-M capture; standalone, no scheduler, keys or orders.

Uses the existing research feed host and the same allowlist discipline. A
snapshot does not prove queue priority, fill probability, or execution.
"""
import argparse,json,math,os,time,urllib.parse,urllib.request
from .adaptive_public_feed import FUTURES
PATHS={'/fapi/v1/depth','/fapi/v1/ticker/bookTicker','/fapi/v1/aggTrades'}
def public(path,params):
 if path not in PATHS or params.get('symbol')!='BTCUSDT' or any(k not in ('symbol','limit') for k in params):raise ValueError('endpoint denied')
 url=FUTURES+path+'?'+urllib.parse.urlencode(params)
 try:
  with urllib.request.urlopen(url,timeout=8) as r:raw=r.read(1000001)
  if len(raw)>1000000:raise ValueError('oversized')
  return json.loads(raw)
 except Exception:raise ValueError('public source unavailable') from None
def capture(fetch=public,clock=lambda:int(time.time()*1000)):
 out={}
 for label,path,params in (('book','/fapi/v1/ticker/bookTicker',{'symbol':'BTCUSDT'}),('depth','/fapi/v1/depth',{'symbol':'BTCUSDT','limit':20}),('trades','/fapi/v1/aggTrades',{'symbol':'BTCUSDT','limit':20})):
  start=clock();data=fetch(path,params);end=clock()
  if end<start or end-start>10000:raise ValueError('capture latency invalid')
  if label=='book' and isinstance(data,dict):event=data.get('time')
  elif label=='depth' and isinstance(data,dict):event=data.get('E')
  elif label=='trades' and isinstance(data,list):event=max((x['T'] for x in data if isinstance(x,dict) and type(x.get('T')) is int),default=None)
  else:event=None
  source_time='absent' if type(event) is not int else 'future_vs_receipt' if event>end else 'reported_unverified'
  out[label]={'request_ms':start,'received_ms':end,'source_event_ms':event if type(event) is int else None,'source_time_status':source_time,'data':data}
 book=out['book']['data'];depth=out['depth']['data'];trades=out['trades']['data']
 if book.get('symbol')!='BTCUSDT' or not 0<float(book['bidPrice'])<float(book['askPrice']):raise ValueError('book invalid')
 if not depth.get('bids') or not depth.get('asks') or not isinstance(trades,list):raise ValueError('depth/trades invalid')
 for row in depth['bids']+depth['asks']:
  if len(row)!=2 or any(not math.isfinite(float(x)) or float(x)<=0 for x in row):raise ValueError('depth invalid')
 return {'schema':1,'symbol':'BTCUSDT','market':'USD-M perpetual','source':'public REST','snapshots':out,'limitations':'Book and trade endpoints are asynchronous; absent source event time remains unknown. No maker-fill inference.'}
def main():
 p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--count',type=int,default=1);p.add_argument('--interval',type=int,default=5);a=p.parse_args()
 if not 1<=a.count<=12 or a.interval<5:raise ValueError('bounded capture only')
 fd=os.open(a.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w',encoding='utf8') as f:
  for i in range(a.count):
   item=capture();f.write(json.dumps(item,sort_keys=True)+'\n');f.flush();os.fsync(f.fileno())
   if i+1<a.count:time.sleep(a.interval)
if __name__=='__main__':main()
