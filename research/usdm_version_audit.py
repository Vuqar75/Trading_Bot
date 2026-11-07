"""Compare archived same-symbol USD-M 5m aggregates to saved 15m versions."""
import hashlib,json,sys
from pathlib import Path
from research.adaptive_data import rows as csvrows
from research.intraday_spot import aggregate,months
from research.usdm_intraday_data import read
new=Path(sys.argv[1]);old=Path(sys.argv[2]);manifest=json.loads((new/'manifest.json').read_text());prior=json.loads((old/'adaptive-futures-manifest.json').read_text());out={}
for kind,prefix in (('klines',''),('markPriceKlines','MARK-')):
 current=aggregate(read(new,manifest,'BTCUSDT',kind),3);by={r[0]:r for r in current};seen={};examples=[];count=0
 for y,m in months():
  name=prefix+f'BTCUSDT-15m-{y}-{m:02d}.zip';meta=next(x for x in prior['files'] if x['file']==name);p=old/name
  if hashlib.sha256(p.read_bytes()).hexdigest()!=meta['sha256']:raise ValueError('prior checksum mismatch '+name)
  for row in csvrows(p):
   if not row[0].isdigit():continue
   t=int(row[0]);seen[t]=tuple(map(float,row[1:5]))
 for item in prior.get('rest_gap_fills',[]):
  if not item['file'].startswith('REST-'+kind+'-'):continue
  raw=(old/item['file']).read_bytes()
  if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('prior REST revision')
  for row in json.loads(raw):seen[int(row[0])]=tuple(map(float,row[1:5]))
 for t,row in by.items():
  if t not in seen:raise ValueError('missing prior 15m '+str(t))
  old_ohlc=seen[t];new_ohlc=row[1:5]
  if old_ohlc!=new_ohlc:
   count+=1
   if len(examples)<8:examples.append({'time_ms':t,'old_ohlc':old_ohlc,'new_ohlc':new_ohlc})
 out[kind]={'bars':len(by),'different_ohlc':count,'examples':examples,'old_source':'BTC USD-M 15m archived '+kind,'new_source':'BTC USD-M 5m archived '+kind+' aggregated to 15m','tolerance':0}
print(json.dumps(out,sort_keys=True))
