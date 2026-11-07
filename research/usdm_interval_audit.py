"""Exact same-instrument 1m-to-5m OHLC version audit; no tolerance."""
import json,sys
from research.intraday_spot import SYMBOLS
from research.usdm_intraday_data import read
root=sys.argv[1];m1=json.load(open(root+'/minute-manifest.json'));m5=json.load(open(root+'/manifest.json'));out={}
for symbol in SYMBOLS:
 for kind in ('klines','markPriceKlines'):
  small=read(root,m1,symbol,kind,interval='1m');large=read(root,m5,symbol,kind);count=0;examples=[]
  if len(small)!=5*len(large):raise ValueError('aggregate coverage mismatch')
  for i,bar in enumerate(large):
   g=small[5*i:5*i+5]
   if g[0][0]!=bar[0] or any(v[0]!=bar[0]+j*60000 for j,v in enumerate(g)):raise ValueError('interval alignment mismatch')
   values=(g[0][1],max(x[2] for x in g),min(x[3] for x in g),g[-1][4])
   if values!=bar[1:5]:
    count+=1
    if len(examples)<3:examples.append({'time_ms':bar[0],'aggregate_1m':values,'archive_5m':bar[1:5]})
  out[symbol+'|'+kind]={'bars':len(large),'different_ohlc':count,'examples':examples,'tolerance':0,'volume_crosscheck':'not performed'}
print(json.dumps(out,sort_keys=True))
